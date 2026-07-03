"""
Tests for usage_limits dependency — service-to-service auth headers,
credit limit enforcement (platform + BYOK paths), and burst guard.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException


MODULE = "src.server.dependencies.usage_limits"


# ===================================================================
# Burst guard tests (_check_burst_guard + release_burst_slot)
# ===================================================================


def _mock_redis_cache(enabled=True, pipeline_results=None, decr_result=0):
    """Return a mock Redis cache for burst guard tests."""
    cache = MagicMock()
    cache.enabled = enabled
    cache.client = MagicMock() if enabled else None

    if enabled and cache.client:
        pipe = AsyncMock()
        pipe.incr = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=pipeline_results or [1])
        cache.client.pipeline = MagicMock(return_value=pipe)
        cache.client.decr = AsyncMock(return_value=decr_result)
        cache.client.set = AsyncMock()

    return cache


class TestCheckBurstGuard:
    """Tests for _check_burst_guard Redis INCR/DECR logic."""

    @pytest.mark.asyncio
    async def test_under_limit_allowed(self):
        """Request under the limit returns allowed=True with correct count."""
        cache = _mock_redis_cache(pipeline_results=[3])

        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            from src.server.dependencies.usage_limits import _check_burst_guard

            result = await _check_burst_guard("user-1", max_concurrent=10)

        assert result["allowed"] is True
        assert result["current"] == 3
        assert result["limit"] == 10

    @pytest.mark.asyncio
    async def test_at_limit_allowed(self):
        """Request at exactly max_concurrent is still allowed."""
        cache = _mock_redis_cache(pipeline_results=[10])

        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            from src.server.dependencies.usage_limits import _check_burst_guard

            result = await _check_burst_guard("user-1", max_concurrent=10)

        assert result["allowed"] is True
        assert result["current"] == 10

    @pytest.mark.asyncio
    async def test_over_limit_rollback(self):
        """Request over limit triggers DECR rollback and returns allowed=False."""
        cache = _mock_redis_cache(pipeline_results=[11])

        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            from src.server.dependencies.usage_limits import _check_burst_guard

            result = await _check_burst_guard("user-1", max_concurrent=10)

        assert result["allowed"] is False
        assert result["current"] == 10
        assert result["limit"] == 10
        cache.client.decr.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_redis_disabled_fail_open(self):
        """When Redis is disabled, burst guard allows the request."""
        cache = _mock_redis_cache(enabled=False)
        cache.client = None

        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            from src.server.dependencies.usage_limits import _check_burst_guard

            result = await _check_burst_guard("user-1", max_concurrent=10)

        assert result["allowed"] is True
        assert "current" not in result

    @pytest.mark.asyncio
    async def test_redis_error_fail_open(self):
        """When Redis raises an exception, burst guard allows the request."""
        cache = _mock_redis_cache(pipeline_results=[1])
        pipe = cache.client.pipeline()
        pipe.execute = AsyncMock(side_effect=ConnectionError("Redis down"))

        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            from src.server.dependencies.usage_limits import _check_burst_guard

            result = await _check_burst_guard("user-1", max_concurrent=10)

        assert result["allowed"] is True


class TestReleaseBurstSlot:
    """Tests for release_burst_slot Redis DECR logic."""

    @pytest.mark.asyncio
    async def test_decr_to_positive(self):
        """Normal release: DECR to a positive value, no clamping."""
        cache = _mock_redis_cache(decr_result=2)

        with (
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
            patch(f"{MODULE}.HOST_MODE", "platform"),
        ):
            from src.server.dependencies.usage_limits import release_burst_slot

            await release_burst_slot("user-1")

        cache.client.decr.assert_awaited_once()
        cache.client.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decr_to_negative_clamps_to_zero(self):
        """When DECR goes negative, clamp the key to 0."""
        cache = _mock_redis_cache(decr_result=-1)

        with (
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
            patch(f"{MODULE}.HOST_MODE", "platform"),
        ):
            from src.server.dependencies.usage_limits import release_burst_slot

            await release_burst_slot("user-1")

        cache.client.decr.assert_awaited_once()
        cache.client.set.assert_awaited_once()
        # Verify it sets to 0
        set_args = cache.client.set.call_args
        assert set_args[0][1] == 0

    @pytest.mark.asyncio
    async def test_redis_error_swallowed(self):
        """Redis errors during release are swallowed (no exception raised)."""
        cache = _mock_redis_cache()
        cache.client.decr = AsyncMock(side_effect=ConnectionError("Redis down"))

        with (
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
            patch(f"{MODULE}.HOST_MODE", "platform"),
        ):
            from src.server.dependencies.usage_limits import release_burst_slot

            # Should not raise
            await release_burst_slot("user-1")


def _mock_cache_miss():
    """Return a mock Redis cache that always misses (get→None, set→no-op)."""
    cache = MagicMock()
    cache.enabled = True
    cache.client = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


@pytest.mark.asyncio
async def test_call_validate_for_user_uses_x_service_token_header():
    """_call_validate_for_user sends X-Service-Token, not Authorization: Bearer."""
    mock_response = httpx.Response(
        200,
        json={"valid": True, "quota": {"allowed": True}},
    )
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch(f"{MODULE}.HOST_MODE", "platform"),
        patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
        patch(f"{MODULE}._get_http_client", return_value=mock_client),
        patch("os.getenv", return_value="my-secret-token"),
    ):
        from src.server.dependencies.usage_limits import _call_validate_for_user

        result = await _call_validate_for_user("user-123", check_quota="chat")

    assert result is not None
    assert result["valid"] is True

    # Verify the actual headers sent
    call_kwargs = mock_client.post.call_args
    headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

    assert "X-Service-Token" in headers
    assert headers["X-Service-Token"] == "my-secret-token"
    assert "Authorization" not in headers
    assert headers["X-User-Id"] == "user-123"


@pytest.mark.asyncio
async def test_call_validate_for_user_no_token_omits_service_header():
    """When INTERNAL_SERVICE_TOKEN is empty, X-Service-Token is not sent."""
    mock_response = httpx.Response(200, json={"valid": True})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch(f"{MODULE}.HOST_MODE", "platform"),
        patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
        patch(f"{MODULE}._get_http_client", return_value=mock_client),
        patch("os.getenv", return_value=""),
    ):
        from src.server.dependencies.usage_limits import _call_validate_for_user

        await _call_validate_for_user("user-456")

    call_kwargs = mock_client.post.call_args
    headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

    assert "X-Service-Token" not in headers
    assert "Authorization" not in headers
    assert headers["X-User-Id"] == "user-456"


@pytest.mark.asyncio
async def test_call_validate_for_user_returns_none_when_no_auth_url():
    """When AUTH_SERVICE_URL is unset, _call_validate_for_user returns None immediately."""
    with patch(f"{MODULE}.AUTH_SERVICE_URL", ""):
        from src.server.dependencies.usage_limits import _call_validate_for_user

        result = await _call_validate_for_user("user-789")

    assert result is None


@pytest.mark.asyncio
async def test_call_validate_for_user_sends_check_quota_in_body():
    """check_quota and byok flags are included in the request body."""
    mock_response = httpx.Response(200, json={"valid": True})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch(f"{MODULE}.HOST_MODE", "platform"),
        patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
        patch(f"{MODULE}._get_http_client", return_value=mock_client),
        patch("os.getenv", return_value="token"),
    ):
        from src.server.dependencies.usage_limits import _call_validate_for_user

        await _call_validate_for_user("user-123", check_quota="workspace", byok=True)

    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

    assert body["check_quota"] == "workspace"
    assert body["byok"] is True


@pytest.mark.asyncio
async def test_call_validate_for_user_fails_open_on_exception():
    """Network errors return None (fail-open)."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with (
        patch(f"{MODULE}.HOST_MODE", "platform"),
        patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
        patch(f"{MODULE}._get_http_client", return_value=mock_client),
        patch("os.getenv", return_value="token"),
    ):
        from src.server.dependencies.usage_limits import _call_validate_for_user

        result = await _call_validate_for_user("user-123")

    assert result is None


# ===================================================================
# Test 4: enforce_credit_limit byok parameter tests
# ===================================================================


class TestEnforceCreditLimitByok:
    """Verify enforce_credit_limit behaviour under byok=True.

    BYOK path goes through _enforce_byok_negative_balance which uses
    Redis cache. Tests mock the cache as a miss so the HTTP call
    to _call_validate_for_user is exercised.
    """

    @pytest.mark.asyncio
    async def test_byok_outstanding_debt_raises_429(self):
        """byok=True with outstanding_debt > 0 raises 429 with type=negative_balance."""
        quota_response = {
            "quota": {
                "allowed": True,
                "outstanding_debt": 100,
                "retry_after": 30,
            }
        }

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=_mock_cache_miss()),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            with pytest.raises(HTTPException) as exc_info:
                await enforce_credit_limit("user-1", byok=True)

            assert exc_info.value.status_code == 429
            assert exc_info.value.detail["type"] == "negative_balance"
            assert exc_info.value.detail["outstanding_debt"] == 100

    @pytest.mark.asyncio
    async def test_byok_unlimited_sentinel_does_not_block(self):
        """Regression: remaining_credits=-1 (unlimited sentinel) MUST NOT block.

        Pre-fix bug: langalpha treated remaining_credits<0 as outstanding debt,
        but ginlix-platform uses -1 for unlimited tiers. This caused BYOK users
        on unlimited plans (or daily-unlimited plans) to be permanently blocked.
        """
        quota_response = {
            "quota": {
                "allowed": True,
                "remaining_credits": -1,  # unlimited sentinel
                "outstanding_debt": 0,
            }
        }

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=_mock_cache_miss()),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            await enforce_credit_limit("user-1", byok=True)

    @pytest.mark.asyncio
    async def test_byok_zero_debt_passes(self):
        """byok=True with outstanding_debt=0 should not raise, even if quota.allowed=False."""
        quota_response = {
            "quota": {
                "allowed": False,
                "remaining_credits": 0,
                "outstanding_debt": 0,
            }
        }

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=_mock_cache_miss()),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            await enforce_credit_limit("user-1", byok=True)

    @pytest.mark.asyncio
    async def test_byok_missing_debt_field_passes(self):
        """Wire-compat: older platform builds without outstanding_debt → no block."""
        quota_response = {
            "quota": {
                "allowed": False,
                "remaining_credits": -1,  # would have blocked under old code
            }
        }

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=_mock_cache_miss()),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            await enforce_credit_limit("user-1", byok=True)

    @pytest.mark.asyncio
    async def test_byok_cache_hit_negative_raises_without_http(self):
        """When cache says 'negative', skip HTTP call entirely and raise 429."""
        cache = _mock_cache_miss()
        cache.get = AsyncMock(return_value="negative")  # cache hit
        mock_validate = AsyncMock()

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", mock_validate),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            with pytest.raises(HTTPException) as exc_info:
                await enforce_credit_limit("user-1", byok=True)

            assert exc_info.value.status_code == 429
            mock_validate.assert_not_called()  # no HTTP call

    @pytest.mark.asyncio
    async def test_byok_cache_hit_ok_passes_without_http(self):
        """When cache says 'ok', skip HTTP call entirely and allow."""
        cache = _mock_cache_miss()
        cache.get = AsyncMock(return_value="ok")  # cache hit
        mock_validate = AsyncMock()

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", mock_validate),
            patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            await enforce_credit_limit("user-1", byok=True)
            mock_validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_byok_allowed_false_raises_429(self):
        """byok=False with allowed=False raises 429."""
        quota_response = {
            "quota": {
                "allowed": False,
                "limit_type": "credit_limit",
                "message": "Daily credit limit reached",
                "remaining_credits": 0,
                "used_credits": 100.0,
                "credit_limit": 100.0,
                "retry_after": 30,
            }
        }

        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            with pytest.raises(HTTPException) as exc_info:
                await enforce_credit_limit("user-1", byok=False)

            assert exc_info.value.status_code == 429
            assert exc_info.value.detail["type"] == "credit_limit"

    @pytest.mark.asyncio
    async def test_non_byok_forwards_platform_message_verbatim(self):
        """Platform message + unknown limit_type pass through unchanged."""
        quota_response = {
            "quota": {
                "allowed": False,
                "limit_type": "some_future_limit",
                "message": "A future limit string from platform",
                "retry_after": 30,
            }
        }
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import enforce_credit_limit
            with pytest.raises(HTTPException) as exc_info:
                await enforce_credit_limit("user-1", byok=False)
            assert exc_info.value.detail["type"] == "some_future_limit"
            assert exc_info.value.detail["message"] == "A future limit string from platform"

    @pytest.mark.asyncio
    async def test_no_auth_service_url_returns_immediately(self):
        """When AUTH_SERVICE_URL is unset, enforce_credit_limit is a no-op."""
        with patch(f"{MODULE}.AUTH_SERVICE_URL", ""):
            from src.server.dependencies.usage_limits import enforce_credit_limit

            await enforce_credit_limit("user-1", byok=True)
            await enforce_credit_limit("user-1", byok=False)


# ===================================================================
# get_capacity_status (read-only count-quota status for the UI)
# ===================================================================


class TestGetCapacityStatus:
    """Verify the display-only capacity reader: parses counts, never raises."""

    @pytest.mark.asyncio
    async def test_oss_mode_returns_none(self):
        """OSS mode has no quotas — returns None without calling the platform."""
        with patch(f"{MODULE}.HOST_MODE", "oss"):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_performance") is None

    @pytest.mark.asyncio
    async def test_no_auth_service_url_returns_none(self):
        """No AUTH_SERVICE_URL configured — returns None immediately."""
        with patch(f"{MODULE}.AUTH_SERVICE_URL", ""):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_max") is None

    @pytest.mark.asyncio
    async def test_parses_capacity_used_and_limit(self):
        """Platform count quota maps to {used, limit}."""
        quota_response = {"quota": {"capacity_used": 1, "capacity_limit": 3}}
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_performance") == {"used": 1, "limit": 3}

    @pytest.mark.asyncio
    async def test_unlimited_sentinel_preserved(self):
        """limit == -1 (unlimited) is passed through, not clamped."""
        quota_response = {"quota": {"capacity_used": 5, "capacity_limit": -1}}
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "always_on") == {"used": 5, "limit": -1}

    @pytest.mark.asyncio
    async def test_unlimited_with_omitted_used(self):
        """Platform omits capacity_used on unlimited tiers — still report limit -1.

        Regression: ginlix-platform's capacity counter returns
        ``QuotaInfo(allowed=True, capacity_limit=-1)`` with no ``capacity_used`` for
        unlimited plans, so requiring ``used`` would hide the "Unlimited" hint.
        """
        quota_response = {"quota": {"allowed": True, "capacity_limit": -1}}
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_performance") == {"used": 0, "limit": -1}

    @pytest.mark.asyncio
    async def test_legacy_field_names_fallback(self):
        """Falls back to active/limit when the platform omits capacity_* aliases."""
        quota_response = {"quota": {"active": 2, "limit": 4}}
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_performance") == {"used": 2, "limit": 4}

    @pytest.mark.asyncio
    async def test_missing_quota_object_returns_none(self):
        """No quota object in the response — degrade to None, no display."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value={"access_tier": 1}),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_performance") is None

    @pytest.mark.asyncio
    async def test_partial_counts_returns_none(self):
        """Quota object present but missing a count field — None rather than a half value."""
        quota_response = {"quota": {"capacity_used": 1}}  # no limit
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "spec_max") is None

    @pytest.mark.asyncio
    async def test_unreachable_platform_returns_none(self):
        """Validate returns None (platform down) — fail soft to None."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=None),
        ):
            from src.server.dependencies.usage_limits import get_capacity_status

            assert await get_capacity_status("user-1", "always_on") is None


# ===================================================================
# always_on_entitlement_lost — the idle-cleanup reconciler's check.
# Fail-safe: only a definitive "no always-on scope" returns True; OSS,
# unreachable, and ambiguous responses keep always-on (return False).
# ===================================================================


class TestAlwaysOnEntitlementLost:
    @pytest.mark.asyncio
    async def test_oss_mode_keeps_always_on(self):
        """OSS mode never reconciles — returns False without calling validate."""
        with (
            patch(f"{MODULE}.HOST_MODE", "oss"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock) as mock_v,
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is False
            mock_v.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scope_present_keeps_always_on(self):
        """200 with the scope present → still entitled → False."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"scopes": ["workspace:always_on", "workspace:spec:max"]},
            ),
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is False

    @pytest.mark.asyncio
    async def test_scope_absent_reports_lost(self):
        """200 with a real scope list lacking always-on → entitlement lost → True."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"scopes": ["workspace:spec:performance"]},
            ),
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is True

    @pytest.mark.asyncio
    async def test_empty_scope_list_reports_lost(self):
        """200 with an explicit empty list (e.g. free tier) → lost → True. The
        list is present, so this is a definitive answer, not an outage."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"scopes": []},
            ),
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is True

    @pytest.mark.asyncio
    async def test_unreachable_keeps_always_on(self):
        """validate None (platform down / non-200) → fail-safe keep → False.
        An outage must never mass-disable always-on."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=None),
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is False

    @pytest.mark.asyncio
    async def test_ambiguous_response_keeps_always_on(self):
        """200 but scopes missing/None (not a list) → ambiguous → fail-safe keep."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"valid": True},
            ),
        ):
            from src.server.dependencies.usage_limits import always_on_entitlement_lost

            assert await always_on_entitlement_lost("user-1") is False


# ===================================================================
# enforce_capacity — the count-quota gate (429). OSS no-op; fail-open
# on unreachable / missing quota; 429 only on an explicit allowed:False.
# ===================================================================


class TestEnforceCapacity:
    @pytest.mark.asyncio
    async def test_oss_mode_is_noop(self):
        """OSS mode never counts — returns without calling the platform."""
        with (
            patch(f"{MODULE}.HOST_MODE", "oss"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock) as mock_v,
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            await enforce_capacity("user-1", "spec_max")
            mock_v.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_auth_service_url_is_noop(self):
        """No AUTH_SERVICE_URL (partial deploy) — fail-open, no platform call."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", ""),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock) as mock_v,
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            await enforce_capacity("user-1", "spec_performance")
            mock_v.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhausted_quota_raises_429_with_platform_fields(self):
        """allowed:False → 429 forwarding the platform's limit_type / counts."""
        quota_response = {
            "quota": {
                "allowed": False,
                "capacity_used": 3,
                "capacity_limit": 3,
                "limit_type": "max_limit",
                "message": "You've used all your max workspaces",
            }
        }
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=quota_response),
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            with pytest.raises(HTTPException) as exc:
                await enforce_capacity("user-1", "spec_max")

            assert exc.value.status_code == 429
            assert exc.value.detail["type"] == "max_limit"
            assert exc.value.detail["current"] == 3
            assert exc.value.detail["limit"] == 3
            assert exc.value.detail["remaining"] == 0
            assert exc.value.headers["X-RateLimit-Remaining"] == "0"

    @pytest.mark.asyncio
    async def test_allowed_quota_passes(self):
        """allowed:True → no raise."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"quota": {"allowed": True}},
            ),
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            await enforce_capacity("user-1", "always_on")  # no exception

    @pytest.mark.asyncio
    async def test_unreachable_platform_fails_open(self):
        """validate None (platform down) → fail-open, never block the user."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock, return_value=None),
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            await enforce_capacity("user-1", "spec_performance")  # no exception

    @pytest.mark.asyncio
    async def test_missing_quota_object_fails_open(self):
        """200 without a quota object → fail-open (no count to enforce)."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._call_validate_for_user",
                new_callable=AsyncMock,
                return_value={"access_tier": 1},
            ),
        ):
            from src.server.dependencies.usage_limits import enforce_capacity

            await enforce_capacity("user-1", "spec_max")  # no exception


# ===================================================================
# assert_spec_allowed — hybrid spec gate (scope 403 + count 429).
# standard is never gated; the count check is skipped when already at
# the target tier (no new slot consumed).
# ===================================================================


class TestAssertSpecAllowed:
    @pytest.mark.asyncio
    async def test_standard_is_noop(self):
        """standard tier is never gated — no scope or count check."""
        with (
            patch(f"{MODULE}._get_user_scopes", new_callable=AsyncMock) as mock_scopes,
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            await assert_spec_allowed("user-1", "standard")
            mock_scopes.assert_not_awaited()
            mock_cap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_performance_missing_scope_raises_403(self):
        """Non-empty scope list lacking the performance scope → 403, no count check."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:spec:max"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            with pytest.raises(HTTPException) as exc:
                await assert_spec_allowed("user-1", "performance", current_tier="standard")

            assert exc.value.status_code == 403
            mock_cap.assert_not_awaited()  # scope failed first

    @pytest.mark.asyncio
    async def test_performance_scope_present_checks_count(self):
        """Scope held + tier change → count quota enforced for spec_performance."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:spec:performance"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            await assert_spec_allowed("user-1", "performance", current_tier="standard")
            mock_cap.assert_awaited_once_with("user-1", "spec_performance")

    @pytest.mark.asyncio
    async def test_already_at_tier_skips_count(self):
        """current_tier == tier → scope checked but no new slot counted."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:spec:max"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            await assert_spec_allowed("user-1", "max", current_tier="max")
            mock_cap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_max_checks_max_scope_and_count(self):
        """max tier gates on the spec:max scope + spec_max count."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:spec:max"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            await assert_spec_allowed("user-1", "max", current_tier="standard")
            mock_cap.assert_awaited_once_with("user-1", "spec_max")

    @pytest.mark.asyncio
    async def test_oss_mode_is_noop(self):
        """OSS mode: scope + count gates both fail open, so any tier is allowed."""
        with (
            patch(f"{MODULE}.HOST_MODE", "oss"),
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock) as mock_v,
            patch(f"{MODULE}._get_user_scopes", new_callable=AsyncMock) as mock_scopes,
        ):
            from src.server.dependencies.usage_limits import assert_spec_allowed

            await assert_spec_allowed("user-1", "max", current_tier="standard")
            mock_v.assert_not_awaited()
            mock_scopes.assert_not_awaited()


# ===================================================================
# assert_always_on_allowed — always-on gate (scope 403 + count 429).
# ===================================================================


class TestAssertAlwaysOnAllowed:
    @pytest.mark.asyncio
    async def test_missing_scope_raises_403(self):
        """Non-empty scope list lacking always-on → 403, count not reached."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:spec:max"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_always_on_allowed

            with pytest.raises(HTTPException) as exc:
                await assert_always_on_allowed("user-1")

            assert exc.value.status_code == 403
            mock_cap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scope_present_checks_count(self):
        """always-on scope held → count quota enforced for always_on."""
        with (
            patch(f"{MODULE}.HOST_MODE", "platform"),
            patch(f"{MODULE}.AUTH_SERVICE_URL", "http://localhost:8003"),
            patch(
                f"{MODULE}._get_user_scopes",
                new_callable=AsyncMock,
                return_value=["workspace:always_on"],
            ),
            patch(f"{MODULE}.enforce_capacity", new_callable=AsyncMock) as mock_cap,
        ):
            from src.server.dependencies.usage_limits import assert_always_on_allowed

            await assert_always_on_allowed("user-1")
            mock_cap.assert_awaited_once_with("user-1", "always_on")

    @pytest.mark.asyncio
    async def test_oss_mode_is_noop(self):
        """OSS mode: both gates fail open — always-on never blocked."""
        with (
            patch(f"{MODULE}.HOST_MODE", "oss"),
            patch(f"{MODULE}._get_user_scopes", new_callable=AsyncMock) as mock_scopes,
            patch(f"{MODULE}._call_validate_for_user", new_callable=AsyncMock) as mock_v,
        ):
            from src.server.dependencies.usage_limits import assert_always_on_allowed

            await assert_always_on_allowed("user-1")
            mock_scopes.assert_not_awaited()
            mock_v.assert_not_awaited()

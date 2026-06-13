import React from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import logoLight from '../../assets/img/logo.svg';
import logoDark from '../../assets/img/logo-dark.svg';
import { useTheme } from '../../contexts/ThemeContext';
import { NAV_ITEMS } from '../nav/navItems';
import { useNavActive } from '../nav/useNavActive';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import AccountMenu from './AccountMenu';
import './Sidebar.css';

function Sidebar() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { theme } = useTheme();
  const isActive = useNavActive();
  const logo = theme === 'light' ? logoDark : logoLight;

  const handleItemClick = (path: string) => {
    navigate(path);
  };

  return (
    <aside className="sidebar">
      {/* Logo */}
      <div className="sidebar-logo" onClick={() => navigate('/dashboard')} style={{ cursor: 'pointer' }}>
        <img src={logo} alt="Logo" style={{ width: '40px', height: '40px', objectFit: 'contain' }} />
      </div>

      {/* Navigation Items */}
      <nav className="sidebar-nav">
        <TooltipProvider delayDuration={300}>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const label = t(item.labelKey);
            const active = isActive(item);

            return (
              <Tooltip key={item.key}>
                <TooltipTrigger asChild>
                  <button
                    className={`sidebar-nav-item ${active ? 'active' : ''}`}
                    onClick={() => handleItemClick(item.key)}
                    aria-label={label}
                  >
                    <Icon className="sidebar-nav-icon" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="right" sideOffset={8}>
                  {label}
                </TooltipContent>
              </Tooltip>
            );
          })}
        </TooltipProvider>
      </nav>

      {/* Account menu — pinned to bottom */}
      <div className="sidebar-bottom">
        <AccountMenu />
      </div>
    </aside>
  );
}

export default Sidebar;

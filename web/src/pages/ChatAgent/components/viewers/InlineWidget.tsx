import { useMemo, useRef, useState } from 'react';
import { buildHtmlSrcDoc } from './html/buildHtmlSrcDoc';
import { useHtmlSandbox } from './html/useHtmlSandbox';
import { useHtmlActions } from './html/useHtmlActions';
import HtmlActionBar from './html/HtmlActionBar';
import HtmlFullscreenModal from './html/HtmlFullscreenModal';
import './InlineWidget.css';

interface InlineWidgetProps {
  html: string;
  title?: string;
  onSendPrompt?: (text: string) => void;
  /** Inline data file contents — injected directly as __WIDGET_DATA__. */
  data?: Record<string, string>;
}

export default function InlineWidget({ html, title, onSendPrompt, data }: InlineWidgetProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [fullscreen, setFullscreen] = useState(false);

  const srcDoc = useMemo(() => buildHtmlSrcDoc('widget-inline', { html, data }), [html, data]);
  const fullscreenSrcDoc = useMemo(
    () => buildHtmlSrcDoc('widget-fullscreen', { html, data }),
    [html, data],
  );

  const { height } = useHtmlSandbox({ iframeRef, autoHeight: true, onSendPrompt });

  const actions = useHtmlActions({ mode: 'widget', srcDoc, fileName: title });
  const fullscreenActions = useHtmlActions({
    mode: 'widget',
    srcDoc: fullscreenSrcDoc,
    fileName: title,
  });

  // No max-height cap — widgets span naturally to fit content (charts, tables,
  // dashboards). The agent controls HTML output and the skill doc guides it to
  // keep widgets reasonable. A cap would add scroll-in-scroll UX that's worse
  // than a tall widget pushing chat down.
  return (
    <div className="inline-widget-container">
      <iframe
        ref={iframeRef}
        srcDoc={srcDoc}
        sandbox="allow-scripts"
        title={title || 'Widget'}
        className="inline-widget-frame"
        style={{
          height: height != null ? `${height}px` : '150px',
          opacity: height != null ? 1 : 0,
        }}
      />
      <HtmlActionBar
        variant="overlay"
        onFullscreen={() => setFullscreen(true)}
        onOpenInNewTab={actions.openInNewTab}
        onDownload={actions.downloadHtml}
        onExportPdf={actions.exportPdf}
      />
      {fullscreen && (
        <HtmlFullscreenModal
          variant="widget"
          open={fullscreen}
          onOpenChange={setFullscreen}
          title={title || 'Widget'}
          srcDoc={fullscreenSrcDoc}
          actions={fullscreenActions}
        />
      )}
    </div>
  );
}

(function () {
  'use strict';
  // Google access is server-configured. Normal users only paste a Doc URL/ID;
  // never show an OAuth/connect control or a credential field in the web UI.
  const remove = () => {
    document.querySelectorAll('button, a').forEach(node => {
      const label = (node.textContent || '').trim().toLowerCase();
      const href = (node.getAttribute('href') || '').toLowerCase();
      if (label === 'connect google' || href.includes('/api/script-work/google/connect')) node.remove();
    });
  };
  remove();
  new MutationObserver(remove).observe(document.body, {childList: true, subtree: true});
})();

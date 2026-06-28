/** Single-open popover coordinator. Header popovers (the folder panel, the context
 *  detail) are independent modules; without coordination two could be open at once
 *  (two cards, two carets). Each registers an idempotent close fn and calls
 *  closeOtherPopovers(its own close) right before opening, so only one is ever open. */
const closers = new Set();

/** Register a popover's close fn. Returns an unregister fn. */
export function registerPopover(close) {
  closers.add(close);
  return () => closers.delete(close);
}

/** Close every registered popover except the one passed in (the one being opened). */
export function closeOtherPopovers(except) {
  closers.forEach((close) => {
    if (close !== except) close();
  });
}

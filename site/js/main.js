/* Hark site — progressive enhancement only. No heavy libs, no webfonts. */

(function () {
  const nav = document.querySelector("[data-nav]");
  if (!nav) return;
  const onScroll = () => {
    nav.classList.toggle("is-scrolled", window.scrollY > 12);
  };
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });
})();

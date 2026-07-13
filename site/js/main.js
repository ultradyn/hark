/* Hark site — progressive enhancement only. */

(function () {
  const nav = document.querySelector("[data-nav]");
  if (nav) {
    const onScroll = () => {
      nav.classList.toggle("is-scrolled", window.scrollY > 12);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  // Ambient waveform (decorative)
  const canvas = document.getElementById("wave");
  if (!canvas || !canvas.getContext) return;

  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resize() {
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  resize();
  window.addEventListener("resize", resize, { passive: true });

  let t0 = performance.now();
  function frame(now) {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const t = (now - t0) / 1000;

    ctx.clearRect(0, 0, w, h);

    // faint baseline
    ctx.strokeStyle = "rgba(238,241,247,0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();

    // dual-layer listening wave
    drawWave(t, w, h, "rgba(232,184,74,0.85)", 1.6, 0.9, 0);
    drawWave(t * 1.15, w, h, "rgba(94,234,212,0.45)", 1.1, 0.55, 1.7);

    // soft center glow pulse
    const pulse = 0.5 + 0.5 * Math.sin(t * 1.4);
    const g = ctx.createRadialGradient(w * 0.5, h * 0.5, 4, w * 0.5, h * 0.5, h * 0.55);
    g.addColorStop(0, `rgba(232,184,74,${0.08 + pulse * 0.06})`);
    g.addColorStop(1, "rgba(232,184,74,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);

    requestAnimationFrame(frame);
  }

  function drawWave(t, w, h, color, ampScale, thick, phase) {
    ctx.strokeStyle = color;
    ctx.lineWidth = thick;
    ctx.lineJoin = "round";
    ctx.beginPath();
    const mid = h / 2;
    const amp = h * 0.22 * ampScale;
    for (let x = 0; x <= w; x += 2) {
      const n = x / w;
      const y =
        mid +
        Math.sin(n * Math.PI * 4 + t * 2.2 + phase) * amp * (0.35 + 0.65 * Math.sin(n * Math.PI)) +
        Math.sin(n * Math.PI * 11 - t * 3.1 + phase) * amp * 0.18;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  requestAnimationFrame(frame);
})();

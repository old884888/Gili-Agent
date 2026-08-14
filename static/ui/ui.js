/* ============================================================================
   Gili UI 动画引擎 —— 可复用 JS 工具

   用法：
   GiliUI.spring({ from, to, velocity, stiffness, damping, onUpdate, onComplete })
   GiliUI.dampedDrag(el, { bounds, onMove, onRelease })
   GiliUI.stagger(container, { selector, step })
   GiliUI.pop(el)  —— 轻弹一下

   自动接管：
   - [data-ui-reveal] 进入视口时加 .is-visible
   - [data-ui-stagger] 的子元素错落入场
   - [data-ui-pop] 点击时触发弹跳
   ============================================================================ */

(function (global) {
  "use strict";

  const raf = global.requestAnimationFrame || ((cb) => setTimeout(() => cb(Date.now()), 16));
  const caf = global.cancelAnimationFrame || clearTimeout;

  function spring(opts) {
    const {
      from = 0,
      to = 1,
      velocity = 0,
      stiffness = 170,
      damping = 26,
      mass = 1,
      onUpdate = () => {},
      onComplete = () => {},
      precision = 0.01,
    } = opts;
    let x = from;
    let v = velocity;
    let frame;
    const step = () => {
      const force = -stiffness * (x - to);
      const accel = (force - damping * v) / mass;
      v += accel / 60;
      x += v / 60;
      onUpdate(x, v);
      if (Math.abs(x - to) < precision && Math.abs(v) < precision) {
        onUpdate(to, 0);
        onComplete();
        return;
      }
      frame = raf(step);
    };
    frame = raf(step);
    return () => caf(frame);
  }

  function dampedDrag(el, opts = {}) {
    if (!el) return () => {};
    const {
      bounds = null,           // {minX, maxX, minY, maxY}
      onMove = () => {},
      onRelease = () => {},
      onStart = () => {},
    } = opts;
    el.classList.add("ui-draggable");
    let active = false;
    let startX = 0, startY = 0;
    let posX = 0, posY = 0;
    let lastX = 0, lastY = 0;
    let velX = 0, velY = 0;
    let cancelSpring = null;

    const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
    const pointerDown = (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      active = true;
      el.classList.add("dragging");
      startX = e.clientX;
      startY = e.clientY;
      posX = lastX = e.clientX;
      posY = lastY = e.clientY;
      velX = velY = 0;
      el.setPointerCapture && el.setPointerCapture(e.pointerId);
      onStart({ x: posX, y: posY });
      e.preventDefault();
    };
    const pointerMove = (e) => {
      if (!active) return;
      velX = e.clientX - lastX;
      velY = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      let x = e.clientX - startX;
      let y = e.clientY - startY;
      if (bounds) {
        x = clamp(x, bounds.minX ?? -Infinity, bounds.maxX ?? Infinity);
        y = clamp(y, bounds.minY ?? -Infinity, bounds.maxY ?? Infinity);
      }
      posX = x;
      posY = y;
      onMove({ x, y, vx: velX, vy: velY, event: e });
    };
    const pointerUp = () => {
      if (!active) return;
      active = false;
      el.classList.remove("dragging");
      if (cancelSpring) cancelSpring();
      // 释放时的阻尼：带惯性衰减，最终回到 onRelease
      let vx = velX, vy = velY;
      cancelSpring = spring({
        from: 0, to: 0, velocity: Math.hypot(vx, vy) * 0.02,
        stiffness: 120, damping: 22, mass: 1,
        onUpdate: (p) => {
          vx *= 0.88;
          vy *= 0.88;
          onMove({ x: posX + vx * 0.3, y: posY + vy * 0.3, vx, vy, inertia: p });
        },
        onComplete: () => onRelease({ x: posX, y: posY }),
      });
    };
    el.addEventListener("pointerdown", pointerDown);
    el.addEventListener("pointermove", pointerMove);
    el.addEventListener("pointerup", pointerUp);
    el.addEventListener("pointercancel", pointerUp);
    return () => {
      el.removeEventListener("pointerdown", pointerDown);
      el.removeEventListener("pointermove", pointerMove);
      el.removeEventListener("pointerup", pointerUp);
      el.removeEventListener("pointercancel", pointerUp);
      if (cancelSpring) cancelSpring();
    };
  }

  function stagger(container, opts = {}) {
    if (!container) return;
    const { selector = ":scope > *", step = 45, base = 0 } = opts;
    const items = Array.from(container.querySelectorAll(selector));
    items.forEach((item, i) => {
      item.style.setProperty("--ui-stagger-delay", `${base + i * step}ms`);
    });
    container.classList.add("ui-stagger");
  }

  function pop(el) {
    if (!el) return;
    el.classList.remove("ui-bounce");
    void el.offsetWidth;
    el.classList.add("ui-bounce");
  }

  function revealOnScroll(root = document, opts = {}) {
    const { threshold = 0.12, rootMargin = "0px 0px -8% 0px" } = opts;
    const targets = root.querySelectorAll("[data-ui-reveal]");
    if (!targets.length) return () => {};
    if (!("IntersectionObserver" in global)) {
      targets.forEach((t) => t.classList.add("is-visible"));
      return () => {};
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const en of entries) {
          if (en.isIntersecting) {
            en.target.classList.add("is-visible");
            io.unobserve(en.target);
          }
        }
      },
      { threshold, rootMargin }
    );
    targets.forEach((t) => io.observe(t));
    return () => io.disconnect();
  }

  function init() {
    revealOnScroll();
    document.querySelectorAll("[data-ui-stagger]").forEach((c) => stagger(c));
    document.addEventListener("click", (e) => {
      const target = e.target.closest("[data-ui-pop]");
      if (target) pop(target);
    });
  }

  const GiliUI = { spring, dampedDrag, stagger, pop, revealOnScroll, init };
  global.GiliUI = GiliUI;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})(window);

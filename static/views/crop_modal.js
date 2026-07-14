/* Crop a square region out of an image, normalised to [0..1].
 *
 * The user can:
 *   - drag inside the crop rect to move it,
 *   - drag a corner handle to resize (square aspect, anchored to the
 *     opposite corner),
 *   - click-and-drag in the dark area outside to start a fresh square,
 *   - hit Reset to go back to the full image.
 *
 * ``onChange(crop)`` fires on every drag tick (used for the realtime preview
 * the contact-edit page renders behind the modal). ``onSave(crop)`` fires
 * once when the user clicks Save.
 */

import { el } from '../util.js';
import { openModal, closeModal } from '../ui.js';


function clamp(v, mn, mx) { return Math.max(mn, Math.min(mx, v)); }


export function openCropModal({ src, initial, onSave, onChange, onClose, title = 'Crop image' }) {
  // Crop is stored as ``{x, y, w, h}`` normalised to the image's natural
  // dimensions. To keep the rendered rect SQUARE in display pixels, we need
  // ``w * imgNaturalWidth == h * imgNaturalHeight`` — equivalently, once we
  // know the surface display size, ``w * surfaceW == h * surfaceH``. So
  // ``h = w * (surfaceW / surfaceH)``. We don't know that ratio until the
  // image loads, so we defer overlay rendering until then.
  let crop = initial ? { ...initial } : { x: 0, y: 0, w: 1, h: 1 };
  let aspect = 1;        // surfaceW / surfaceH (a.k.a. displayed image)
  let surfaceW = 1;
  let surfaceH = 1;

  const surface = el('div', { class: 'crop-surface' });
  const img = el('img', { src, alt: '', draggable: false });
  surface.append(img);

  // The dark backdrop and the rect-with-handles live in two separate
  // siblings so we can clip the backdrop to the image area while letting the
  // handles poke out past it.
  const shadow = el('div', { class: 'crop-shadow' });
  surface.append(el('div', { class: 'crop-clip' }, shadow));

  const rect = el('div', { class: 'crop-rect' });
  for (const corner of ['tl', 'tr', 'bl', 'br']) {
    rect.append(el('div', { class: `crop-handle ${corner}`, dataset: { corner } }));
  }
  surface.append(rect);

  /* Convert a rect width-in-normalised-x to its matching height-in-normalised-y
   * such that the rect is SQUARE in display pixels. */
  const matchH = (w) => w * aspect;
  const matchW = (h) => h / aspect;

  function sanitize(c) {
    let w = clamp(+(c.w ?? 1), 0.02, 1);
    let h = matchH(w);
    if (h > 1) { h = 1; w = matchW(h); }
    let x = clamp(+(c.x ?? 0), 0, 1 - w);
    let y = clamp(+(c.y ?? 0), 0, 1 - h);
    return { x, y, w, h };
  }

  function applyToOverlay() {
    const cssLeft = `${crop.x * 100}%`;
    const cssTop = `${crop.y * 100}%`;
    const cssW = `${crop.w * 100}%`;
    const cssH = `${crop.h * 100}%`;
    rect.style.left = cssLeft;
    rect.style.top = cssTop;
    rect.style.width = cssW;
    rect.style.height = cssH;
    shadow.style.left = cssLeft;
    shadow.style.top = cssTop;
    shadow.style.width = cssW;
    shadow.style.height = cssH;
    if (onChange) onChange(crop);
  }

  function recomputeAspect() {
    const r = surface.getBoundingClientRect();
    surfaceW = r.width || 1;
    surfaceH = r.height || 1;
    aspect = surfaceW / surfaceH;
    crop = sanitize(crop);
    applyToOverlay();
  }

  img.addEventListener('load', recomputeAspect);
  if (img.complete && img.naturalWidth > 0) {
    // Image already loaded (cached); compute on next frame.
    requestAnimationFrame(recomputeAspect);
  }

  /* Drag handling */
  let drag = null;

  function pointerToNorm(e) {
    const r = surface.getBoundingClientRect();
    return {
      x: clamp((e.clientX - r.left) / r.width, 0, 1),
      y: clamp((e.clientY - r.top) / r.height, 0, 1),
    };
  }

  surface.addEventListener('pointerdown', (e) => {
    const p = pointerToNorm(e);
    const corner = e.target?.dataset?.corner;
    if (corner) {
      drag = { mode: 'resize', corner, startCrop: { ...crop } };
    } else if (p.x >= crop.x && p.x <= crop.x + crop.w
            && p.y >= crop.y && p.y <= crop.y + crop.h) {
      drag = { mode: 'move', startCrop: { ...crop }, startP: p };
    } else {
      drag = { mode: 'draw', startP: p };
      crop = sanitize({ x: p.x, y: p.y, w: 0.05, h: matchH(0.05) });
      applyToOverlay();
    }
    e.preventDefault();
    try { surface.setPointerCapture(e.pointerId); } catch {}
  });

  surface.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const p = pointerToNorm(e);

    if (drag.mode === 'move') {
      const dx = p.x - drag.startP.x;
      const dy = p.y - drag.startP.y;
      crop = {
        x: clamp(drag.startCrop.x + dx, 0, 1 - drag.startCrop.w),
        y: clamp(drag.startCrop.y + dy, 0, 1 - drag.startCrop.h),
        w: drag.startCrop.w,
        h: drag.startCrop.h,
      };
    } else if (drag.mode === 'resize') {
      const sc = drag.startCrop;
      let anchorX, anchorY;
      if (drag.corner === 'tl') { anchorX = sc.x + sc.w; anchorY = sc.y + sc.h; }
      else if (drag.corner === 'tr') { anchorX = sc.x;       anchorY = sc.y + sc.h; }
      else if (drag.corner === 'bl') { anchorX = sc.x + sc.w; anchorY = sc.y; }
      else                           { anchorX = sc.x;       anchorY = sc.y; }

      // Compute size in *display* pixels and constrain to a square.
      const dxPx = Math.abs(p.x - anchorX) * surfaceW;
      const dyPx = Math.abs(p.y - anchorY) * surfaceH;
      let sizePx = Math.max(2, Math.min(dxPx, dyPx));

      let w = sizePx / surfaceW;
      let h = sizePx / surfaceH;

      let newX = p.x >= anchorX ? anchorX : anchorX - w;
      let newY = p.y >= anchorY ? anchorY : anchorY - h;

      // Clamp to image bounds, shrinking the rect if necessary while keeping
      // it square in display pixels.
      if (newX < 0) {
        w = Math.min(w, anchorX);
        h = matchH(w);
        newX = p.x >= anchorX ? anchorX : 0;
        newY = p.y >= anchorY ? anchorY : anchorY - h;
      }
      if (newY < 0) {
        h = Math.min(h, anchorY);
        w = matchW(h);
        newY = p.y >= anchorY ? anchorY : 0;
        newX = p.x >= anchorX ? anchorX : anchorX - w;
      }
      if (newX + w > 1) { w = 1 - newX; h = matchH(w); }
      if (newY + h > 1) { h = 1 - newY; w = matchW(h); }

      crop = { x: newX, y: newY, w, h };
    } else if (drag.mode === 'draw') {
      const sx = drag.startP.x, sy = drag.startP.y;
      const dx = p.x - sx, dy = p.y - sy;
      const sizePx = Math.max(2, Math.min(Math.abs(dx) * surfaceW, Math.abs(dy) * surfaceH));
      const w = sizePx / surfaceW;
      const h = sizePx / surfaceH;
      const x = dx >= 0 ? sx : sx - w;
      const y = dy >= 0 ? sy : sy - h;
      crop = sanitize({ x, y, w, h });
    }
    applyToOverlay();
  });

  function endDrag() { drag = null; }
  surface.addEventListener('pointerup', endDrag);
  surface.addEventListener('pointercancel', endDrag);

  const body = el('div', {},
    el('h3', {}, title),
    el('p', { style: { color: 'var(--text-mute)', fontSize: '0.857rem', marginTop: '-6px' } },
      'Drag the box to move it, drag a corner to resize, or click outside to draw a new one.'),
    el('div', { class: 'crop-surface-wrap' }, surface),
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn ghost',
        onClick: () => { crop = sanitize({ x: 0, y: 0, w: 1, h: matchH(1) }); applyToOverlay(); },
      }, 'Reset'),
      el('span', { style: { flex: 1 } }),
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', {
        class: 'btn primary',
        onClick: async () => {
          // Await onSave so any server regeneration finishes before the
          // modal closes; the caller's onClose then refreshes against the
          // freshly-rebuilt display image.
          try { if (onSave) await onSave(crop); }
          finally { closeModal(); }
        },
      }, 'Save'),
    ),
  );
  openModal(body, { size: 'large', onClose });
}

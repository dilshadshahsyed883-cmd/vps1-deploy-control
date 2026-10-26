/* Scene Builder: CapCut Controls > Combo / Cam Motion gallery. */
(() => {
  'use strict';
  const root = document.getElementById('sb-capcut-animation');
  if (!root) return;
  const form = document.getElementById('settings-form');
  const categoryInput = document.getElementById('sb-animation-category');
  const effectInput = document.getElementById('sb-animation-effect');
  const durationInput = document.getElementById('sb-animation-duration');
  const intensityInput = document.getElementById('sb-animation-intensity');
  const filterInput = document.getElementById('sb-animation-search');
  const onlyFavInput = document.getElementById('sb-animation-favorites-only');
  const image = document.getElementById('sb-capcut-preview-image');
  const summary = document.getElementById('sb-animation-status');
  const cards = [...root.querySelectorAll('[data-animation-card]')];
  const allEffects = new Set(cards.map(el => el.dataset.animationCard));
  const selection = {combo:'none',cam_motion:'none'};
  const savedCategory = categoryInput.value === 'cam_motion' ? 'cam_motion' : 'combo';
  selection[savedCategory] = allEffects.has(effectInput.value) ? effectInput.value : 'none';
  const motionNames = {
    zoom_1:'sb-cam-zoom',bend_zoom:'sb-cam-drift',distort_left:'sb-cam-drift',
    pendulum_1:'sb-cam-swing',pendulum_2:'sb-cam-swing',
    spin_fall:'sb-cam-fall',bouncy_rush:'sb-cam-bounce',zoom_2:'sb-cam-pulse',
    distort_right:'sb-cam-drift',sway_in:'sb-cam-enter',
    roll_in_out_2:'sb-cam-swing',sway_out:'sb-cam-exit',
    fall_right:'sb-cam-fall',bounce_1:'sb-cam-bounce',wobble:'sb-cam-wobble',
    pan_left:'sb-cam-panleft',pan_right:'sb-cam-panright',
    right_zoom:'sb-cam-rightzoom',dynamic_shake_2:'sb-cam-shake'
  };
  let favorites = new Set();
  try {
    const loaded = JSON.parse(localStorage.getItem('sb-capcut-animation-favorites-v1') || '[]');
    if (Array.isArray(loaded)) favorites = new Set(loaded.filter(x => allEffects.has(x)));
  } catch (_) {}
  let playing = true;

  function autosave(input) { input.dispatchEvent(new Event('change',{bubbles:true})); }
  function show() {
    const category = categoryInput.value;
    const term = filterInput.value.trim().toLowerCase();
    root.querySelectorAll('[data-animation-category]').forEach(btn => {
      btn.setAttribute('aria-pressed',String(btn.dataset.animationCategory===category));
    });
    cards.forEach(card => {
      const effect = card.dataset.animationCard;
      const inCategory = card.dataset.category === category || effect === 'none';
      card.hidden = !inCategory ||
        (Boolean(term) && !card.dataset.label.toLowerCase().includes(term)) ||
        (onlyFavInput.checked && effect !== 'none' && !favorites.has(effect));
      const select = card.querySelector('[data-animation-select]');
      const star = card.querySelector('[data-animation-star]');
      select?.setAttribute('aria-pressed',String(effect===effectInput.value));
      if (star) {
        star.setAttribute('aria-pressed',String(favorites.has(effect)));
        star.setAttribute('aria-label',(favorites.has(effect)?'Remove ':'Add ') + card.dataset.label + (favorites.has(effect)?' from':' to') + ' favorites');
        star.textContent = favorites.has(effect) ? '★' : '☆';
      }
    });
    const dur = Number(durationInput.value);
    const intensity = Number(intensityInput.value);
    document.getElementById('sb-animation-duration-value').textContent = dur.toFixed(1) + 's';
    document.getElementById('sb-animation-intensity-value').textContent = intensity + '%';
    const selectedCard = cards.find(el => el.dataset.animationCard===effectInput.value && !el.hidden);
    const label = effectInput.value==='none'?'None':(selectedCard?.dataset.label || effectInput.value.replaceAll('_',' '));
    summary.textContent = label + ' · ' + dur.toFixed(1) + 's · Intensity ' + intensity + '% · illustrative preview';
    if (image) {
      image.style.animation = 'none';
      image.classList.toggle('sb-animation-active',effectInput.value!=='none');
      image.style.scale = effectInput.value==='none' ? '' : String(0.94 + intensity / 1100);
      if (effectInput.value!=='none') {
        void image.offsetWidth;
        image.style.animation = (motionNames[effectInput.value]||'sb-cam-zoom') + ' ' + dur + 's ease-in-out infinite alternate';
        image.style.animationPlayState = playing ? 'running' : 'paused';
      } else {
        image.style.animation = '';
      }
    }
    document.getElementById('sb-animation-play').textContent = playing?'Pause preview':'Play preview';
  }

  root.querySelectorAll('[data-animation-category]').forEach(btn => btn.addEventListener('click',() => {
    const category = btn.dataset.animationCategory;
    if (categoryInput.value===category) return;
    selection[categoryInput.value] = effectInput.value;
    categoryInput.value = category;
    effectInput.value = selection[category] || 'none';
    autosave(categoryInput);
    autosave(effectInput);
    show();
  }));
  cards.forEach(card => {
    const effect = card.dataset.animationCard;
    card.querySelector('[data-animation-select]')?.addEventListener('click',() => {
      if (effectInput.value!==effect) {
        effectInput.value = effect;
        selection[categoryInput.value] = effect;
        autosave(effectInput);
      }
      playing = true;
      show();
    });
    card.querySelector('[data-animation-star]')?.addEventListener('click',() => {
      if (favorites.has(effect)) favorites.delete(effect);
      else favorites.add(effect);
      try {localStorage.setItem('sb-capcut-animation-favorites-v1',JSON.stringify([...favorites]));} catch (_) {}
      show();
    });
  });
  filterInput.addEventListener('input',show);
  onlyFavInput.addEventListener('change',show);
  [durationInput,intensityInput].forEach(el => el.addEventListener('input',show));
  document.getElementById('sb-animation-play').addEventListener('click',() => {
    playing = !playing;
    if (image && effectInput.value!=='none') image.style.animationPlayState = playing ? 'running' : 'paused';
    document.getElementById('sb-animation-play').textContent = playing?'Pause preview':'Play preview';
  });
  document.getElementById('sb-animation-replay').addEventListener('click',() => {playing=true;show();});
  document.getElementById('sb-animation-clear').addEventListener('click',() => {
    effectInput.value='none';
    selection[categoryInput.value]='none';
    autosave(effectInput);
    show();
  });
  // Existing settings form handles auto-save and explicit submit; no new endpoint.
  show();
})();

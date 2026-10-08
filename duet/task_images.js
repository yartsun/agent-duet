/* Clipboard images stay in the draft until the user submits the task. */
'use strict';
class TaskImages {
  constructor({dialog, preview, status, error, onBusy}) {
    this.dialog = dialog; this.preview = preview; this.status = status;
    this.error = error; this.onBusy = onBusy; this.items = [];
    this.pending = 0; this.generation = 0; this.chain = Promise.resolve(); this.locked = false;
  }
  paste(event) {
    if (!this.dialog.open) return;
    const files = Array.from(event.clipboardData?.items || [])
      .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
      .map(item => item.getAsFile()).filter(Boolean);
    if (!files.length) return; // Normal text paste is handled by the browser.
    event.preventDefault(); this.add(files);
  }
  add(files) {
    if (this.locked) return;
    const generation = this.generation;
    this.pending++; this.onBusy(true); this.render();
    this.chain = this.chain.then(async () => {
      for (const file of files) {
        if (generation !== this.generation) break;
        try {
          if (this.items.length >= 10) throw new Error('Up to 10 images per task.');
          const item = await this.encode(file);
          if (generation !== this.generation) { URL.revokeObjectURL(item.url); break; }
          if (this.items.reduce((sum, value) => sum + value.size, 0) + item.size > 20 * 1024 * 1024) {
            URL.revokeObjectURL(item.url); throw new Error('Images may total at most 20 MB.');
          }
          this.items.push(item);
        } catch (error) { if (generation === this.generation) this.error(error.message); }
      }
    }).finally(() => {
      this.pending--; this.onBusy(this.pending > 0); this.render();
    });
    return this.chain;
  }
  async encode(file) {
    if (!file.type.startsWith('image/') || file.size > 20 * 1024 * 1024)
      throw new Error('Choose an image up to 20 MB.');
    const source = URL.createObjectURL(file);
    const image = new Image();
    try {
      await new Promise((resolve, reject) => {
        image.onload = resolve; image.onerror = () => reject(new Error('Could not read the image. Try PNG, JPEG or WebP.'));
        image.src = source;
      });
      const scale = Math.min(1, 2048 / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob || blob.size > 5 * 1024 * 1024) throw new Error('The prepared image is larger than 5 MB. Make it smaller.');
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('Could not prepare the image.')); reader.readAsDataURL(blob);
      });
      return {name: (file.name || 'Pasted image').replace(/[\x00-\x1f]/g, '').slice(0, 120) || 'Image',
        size: blob.size, data, url: URL.createObjectURL(blob)};
    } finally { image.src = ''; URL.revokeObjectURL(source); }
  }
  render() {
    this.preview.replaceChildren();
    this.items.forEach((item, index) => {
      const card = document.createElement('div'); card.className = 'photo-card';
      const image = document.createElement('img'); image.src = item.url; image.alt = item.name;
      const label = document.createElement('span'); label.textContent = item.name; label.title = item.name;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
      remove.setAttribute('aria-label', 'Remove image ' + (index + 1)); remove.disabled = this.locked;
      remove.addEventListener('click', () => {
        URL.revokeObjectURL(item.url); this.items.splice(this.items.indexOf(item), 1); this.render();
      });
      card.append(image, label, remove); this.preview.append(card);
    });
    this.status.textContent = this.pending ? 'Preparing images…' : this.items.length
      ? 'Images: ' + this.items.length + '/10. The builder and the reviewer will see them.' : '';
  }
  payload() { return this.items.map(({name, data}) => ({name, data})); }
  lock(value) { this.locked = value; this.render(); }
  clear() {
    this.generation++; this.items.forEach(item => URL.revokeObjectURL(item.url)); this.items = []; this.render();
  }
}
window.TaskImages = TaskImages;

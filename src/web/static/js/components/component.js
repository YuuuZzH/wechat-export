// component.js — base class for all UI components
class Component {
  constructor(el) {
    this.el = el;
  }

  template(data) {
    return '';
  }

  render(data) {
    this.el.innerHTML = this.template(data);
  }

  mount() {}
  destroy() {}

  show() {
    this.el.style.display = '';
  }

  hide() {
    this.el.style.display = 'none';
  }
}

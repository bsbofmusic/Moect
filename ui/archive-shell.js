const MoectArchive = (() => {
  function mountHeader() {
    const panel = document.getElementById('header-panel');
    const header = panel.parentElement;
    const row = document.createElement('div');
    row.className = 'header-toggle-row';
    row.innerHTML = `<button id="toggle-header" type="button" aria-controls="header-panel" aria-expanded="true" title="收起顶栏">
      <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 12 5-5 5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="sr-only">收起顶栏</span>
    </button>`;
    header.append(row);
    const toggle = row.firstElementChild;
    toggle.addEventListener('click', () => {
      const collapsed = header.classList.toggle('is-collapsed');
      panel.inert = collapsed;
      panel.setAttribute('aria-hidden', String(collapsed));
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.title = collapsed ? '展开顶栏' : '收起顶栏';
      toggle.querySelector('span').textContent = toggle.title;
    });
  }

  function paginationModel(count, current, pageSize, group = null) {
    const pages = Math.ceil(Math.max(0, count) / pageSize);
    const last = Math.max(0, pages - 1);
    const selected = Math.max(0, Math.min(current, last));
    const start = 10 * Math.max(0, Math.min(Math.floor((group ?? selected) / 10), Math.floor(last / 10)));
    const numbers = Array.from({length: Math.min(10, Math.max(0, pages - start))}, (_, index) => start + index + 1);
    return {pages, selected, start, numbers, previousGroup: start > 0, nextGroup: start + 10 < pages};
  }

  function mountPager({pageSize, label, onPage}) {
    const footer = document.getElementById('archive-pagination');
    footer.innerHTML = `<div class="pagination-controls">
      <nav id="pagination">
        <button id="prev" type="button" aria-label="上一页">‹</button>
        <button id="prev-group" type="button" hidden>上一列</button>
        <span id="page-buttons"></span>
        <button id="next-group" type="button" hidden>下一列</button>
        <button id="next" type="button" aria-label="下一页">›</button>
      </nav>
      <div id="page-jump-controls" class="page-jump" role="group" aria-label="跳转到指定页" hidden>
        <label>跳至 <input id="page-jump" type="number" min="1" inputmode="numeric" aria-label="跳转页码" aria-describedby="page-jump-error"> 页</label>
        <button id="jump-go" type="button">跳转</button>
      </div>
    </div><div id="page" role="status"></div><div id="page-jump-error" role="status"></div>`;

    const byId = id => document.getElementById(id);
    const prev = byId('prev'), next = byId('next'), prevGroup = byId('prev-group'), nextGroup = byId('next-group');
    const pageButtons = byId('page-buttons'), jump = byId('page-jump'), jumpGo = byId('jump-go');
    const jumpControls = byId('page-jump-controls'), pageLabel = byId('page'), jumpError = byId('page-jump-error');
    byId('pagination').setAttribute('aria-label', label);
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(() => document.documentElement.style.setProperty('--archive-bottom-space', footer.offsetHeight + 16 + 'px')).observe(footer);
    }

    let count = 0, page = 0, group = 0, busy = false;
    function clearJumpError() {
      jumpError.textContent = '';
      jump.removeAttribute('aria-invalid');
    }
    function render(align) {
      const model = paginationModel(count, page, pageSize, align ? null : group);
      page = model.selected;
      group = model.start;
      pageButtons.replaceChildren();
      for (const number of model.numbers) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = String(number);
        button.setAttribute('aria-label', '第 ' + number + ' 页');
        if (number === page + 1) button.setAttribute('aria-current', 'page');
        button.disabled = busy;
        button.onclick = () => onPage(number - 1);
        pageButtons.append(button);
      }
      jumpControls.hidden = model.pages < 20;
      jump.value = String(count ? page + 1 : 1);
      jump.max = String(Math.max(1, model.pages));
      jump.disabled = jumpGo.disabled = busy || !count;
      clearJumpError();
      pageLabel.textContent = '第 ' + (count ? page + 1 : 0) + ' / ' + model.pages + ' 页';
      prev.disabled = busy || page === 0;
      next.disabled = busy || page + 1 >= model.pages;
      prevGroup.hidden = nextGroup.hidden = model.pages <= 10;
      prevGroup.disabled = busy || !model.previousGroup;
      nextGroup.disabled = busy || !model.nextGroup;
    }
    prev.onclick = () => onPage(page - 1);
    next.onclick = () => onPage(page + 1);
    prevGroup.onclick = () => { group -= 10; render(false); };
    nextGroup.onclick = () => { group += 10; render(false); };
    jumpGo.onclick = () => {
      const requested = Number(jump.value);
      if (!Number.isInteger(requested) || requested < 1 || requested > Math.ceil(count / pageSize)) {
        jumpError.textContent = '请输入有效页码';
        jump.setAttribute('aria-invalid', 'true');
        jump.focus({preventScroll: true});
        return;
      }
      onPage(requested - 1);
    };
    jump.oninput = clearJumpError;
    jump.onkeydown = event => { if (event.key === 'Enter') jumpGo.click(); };

    return {
      update(nextCount, nextPage, reading = false, align = true) {
        count = nextCount;
        page = nextPage;
        busy = reading;
        render(align);
      },
    };
  }
  return {mountHeader, mountPager, paginationModel};
})();

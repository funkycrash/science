(function () {
  var input = document.getElementById('q');
  var results = document.getElementById('results');
  var count = document.getElementById('count');
  if (!input || !results) return;

  var data = null;

  function fold(s) {
    return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function render(items, query) {
    results.innerHTML = items.map(function (a) {
      var kind = a.k ? ' <span class="row__kind small">' + escapeHtml(a.k) + '</span>' : '';
      var themes = a.th.length ? '<p>' + escapeHtml(a.th.join(' · ')) + '</p>' : '';
      return '<li class="row row--stack"><time class="small" datetime="' + a.d + '">' + a.f + '</time>' +
        '<div><a href="' + a.u + '">' + escapeHtml(a.t) + '</a>' + kind +
        '<p>' + escapeHtml(a.s) + '</p>' + themes + '</div></li>';
    }).join('');
    if (!query) {
      count.textContent = data.length + ' articles. Tapez un mot pour filtrer.';
    } else if (items.length === 0) {
      count.textContent = 'Aucun article ne correspond à « ' + query + ' ».';
    } else {
      count.textContent = items.length + (items.length > 1 ? ' articles' : ' article');
    }
  }

  function search() {
    if (!data) return;
    var query = input.value.trim();
    var words = fold(query).split(/\s+/).filter(Boolean);
    if (words.length === 0) {
      render(data.slice(0, 30), '');
      return;
    }
    var hits = data.filter(function (a) {
      return words.every(function (w) { return a._f.indexOf(w) !== -1; });
    });
    render(hits, query);
  }

  fetch('/search.json')
    .then(function (r) { return r.json(); })
    .then(function (json) {
      data = json.map(function (a) {
        a._f = fold([a.t, a.s, a.k, a.th.join(' ')].join(' '));
        return a;
      });
      var initial = new URLSearchParams(location.search).get('q');
      if (initial) input.value = initial;
      search();
    });

  input.addEventListener('input', search);
})();

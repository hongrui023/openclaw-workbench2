/* 科研 & 生活工作台 · 前端逻辑
 *
 * 三条纪律：
 *  1. 所有动态内容（文件名、模型输出、记录文本）一律用 textContent 写入，
 *     绝不用 innerHTML —— 文件名与模型输出都是不可信输入。
 *  2. 轮询间隔取服务端的建议值（默认 30 秒），不要改小。
 *     穿透通道按流量计费，2 秒轮询会让消耗翻十几倍。
 *  3. 本文件里不出现 AI 服务的名称、地址、端口、模型标识或令牌。
 *     浏览器从头到尾不知道后端在和谁说话——这一条由自检脚本强制检查。
 */
(function () {
  'use strict';

  var CSRF_HEADERS = { 'X-Requested-With': 'owb' };

  var state = {
    pollSeconds: 30,
    files: [],
    selected: {},
    jobId: null,
    timer: null,
    pollTicks: 0
  };

  function $(id) { return document.getElementById(id); }

  function api(path, options) {
    var opts = options || {};
    var headers = Object.assign({}, CSRF_HEADERS, opts.headers || {});
    var init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: headers,
      cache: 'no-store'
    };
    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
      headers['Content-Type'] = 'application/json';
    }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var err = new Error((data && data.message) || '请求失败（HTTP ' + res.status + '）');
          err.code = data && data.code;
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  var toastTimer = null;
  function toast(message) {
    var el = $('toast');
    el.textContent = message;
    el.classList.remove('hidden');
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { el.classList.add('hidden'); }, 3200);
  }

  function showError(el, message) {
    el.textContent = message;
    el.classList.remove('hidden');
  }

  function hide(el) { el.classList.add('hidden'); }

  function setScreen(name) {
    $('login').classList.toggle('hidden', name !== 'login');
    $('app').classList.toggle('hidden', name !== 'app');
  }

  /* ---------------- 登录 ---------------- */

  function boot() {
    api('/api/auth/me').then(function (me) {
      if (me && me.authenticated) {
        enterApp(me);
      } else {
        setScreen('login');
        if (me && me.auth_configured === false) {
          showError($('login-error'),
            '服务端尚未配置登录口令（WORKBENCH_PASSWORD_HASH），现在无法登录。请按 docs/DEPLOY-NAS.md 配置后重启容器。');
        }
        $('password').focus();
      }
    }).catch(function (err) {
      setScreen('login');
      showError($('login-error'), err.message);
    });
  }

  function enterApp(me) {
    setScreen('app');
    state.pollSeconds = (me && me.poll_seconds) || 30;

    var ai = $('ai-state');
    if (me && me.ai_configured === false) {
      ai.textContent = 'AI 未配置';
      ai.className = 'small';
      ai.classList.add('badge', 'warn');
    } else {
      ai.textContent = (me && me.model) ? ('AI: ' + me.model) : '';
      ai.className = 'small muted';
    }

    loadFiles();
    loadNotes();
  }

  $('login-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var button = $('login-btn');
    var errorBox = $('login-error');
    hide(errorBox);
    button.disabled = true;
    button.textContent = '登录中…';
    api('/api/auth/login', { method: 'POST', body: { password: $('password').value } })
      .then(function () {
        $('password').value = '';
        return api('/api/auth/me');
      })
      .then(function (me) { enterApp(me); })
      .catch(function (err) { showError(errorBox, err.message); })
      .finally(function () {
        button.disabled = false;
        button.textContent = '登录';
      });
  });

  $('logout-btn').addEventListener('click', function () {
    api('/api/auth/logout', { method: 'POST' }).catch(function () { /* 忽略 */ }).finally(function () {
      stopPolling();
      setScreen('login');
      $('password').focus();
    });
  });

  /* ---------------- 标签页 ---------------- */

  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (tab) {
    tab.addEventListener('click', function () {
      Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (other) {
        other.classList.toggle('active', other === tab);
      });
      var name = tab.getAttribute('data-tab');
      $('panel-lit').classList.toggle('hidden', name !== 'lit');
      $('panel-notes').classList.toggle('hidden', name !== 'notes');
      $('panel-log').classList.toggle('hidden', name !== 'log');
      if (name === 'notes') { loadNotes(); }
      if (name === 'log') { loadLog(); }
    });
  });

  /* ---------------- 文献列表 ---------------- */

  function formatSize(kb) {
    if (kb >= 1024) { return (kb / 1024).toFixed(1) + ' MB'; }
    return Math.max(1, Math.round(kb)) + ' KB';
  }

  function loadFiles() {
    var list = $('filelist');
    list.textContent = '';
    var loading = document.createElement('p');
    loading.className = 'muted small';
    loading.textContent = '正在读取 literature 目录…';
    list.appendChild(loading);

    api('/api/literature/files').then(function (data) {
      state.files = (data && data.files) || [];
      state.selected = {};
      renderFiles(data);
    }).catch(function (err) {
      list.textContent = '';
      var p = document.createElement('p');
      p.className = 'error';
      p.textContent = err.message;
      list.appendChild(p);
      $('lit-summary').textContent = '';
    });
  }

  function renderFiles(data) {
    var list = $('filelist');
    list.textContent = '';
    var files = state.files;

    $('lit-summary').textContent = files.length
      ? ('共 ' + files.length + ' 篇 PDF，其中 ' + (data.pending || 0) + ' 篇尚未生成 Markdown。')
      : 'literature 目录中还没有 PDF。请用极空间的客户端或 SMB 把论文放进去（不走网页上传，省穿透流量）。';

    if (!files.length) { updateAnalyzeButton(); return; }

    files.forEach(function (file) {
      var row = document.createElement('div');
      row.className = 'file-row';

      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !!file.has_markdown === false;
      box.addEventListener('change', function () {
        if (box.checked) { state.selected[file.name] = true; } else { delete state.selected[file.name]; }
        updateAnalyzeButton();
      });
      if (!file.has_markdown) { state.selected[file.name] = true; }

      var name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = file.name;          // 不可信输入，只用 textContent

      var meta = document.createElement('span');
      meta.className = 'file-meta';
      meta.textContent = formatSize(file.size_kb);

      var badge = document.createElement('span');
      badge.className = 'badge ' + (file.has_markdown ? 'ok' : 'warn');
      badge.textContent = file.has_markdown ? '已有分析' : '待分析';
      if (file.has_markdown) {
        badge.classList.add('clickable');
      }

      var view = document.createElement('button');
      view.type = 'button';
      view.className = 'btn ghost small';
      view.textContent = '查看';
      view.disabled = !file.has_markdown;
      view.addEventListener('click', function () {
        viewMarkdown(file.name.replace(/\.pdf$/i, '') + '.md');
      });

      row.appendChild(box);
      row.appendChild(name);
      row.appendChild(meta);
      row.appendChild(badge);
      row.appendChild(view);
      list.appendChild(row);
    });

    updateAnalyzeButton();
  }

  function updateAnalyzeButton() {
    var count = Object.keys(state.selected).length;
    var button = $('analyze-selected');
    button.disabled = count === 0;
    button.textContent = count ? ('分析选中的 ' + count + ' 篇 PDF') : '分析选中的 PDF';
  }

  $('refresh-files').addEventListener('click', loadFiles);

  $('analyze-selected').addEventListener('click', function () {
    var names = Object.keys(state.selected);
    if (!names.length) { toast('请先勾选要分析的 PDF'); return; }
    submitAnalyze({ scope: 'files', files: names, force: $('force').checked });
  });

  $('analyze-all').addEventListener('click', function () {
    submitAnalyze({ scope: 'all', files: [], force: $('force').checked });
  });

  function submitAnalyze(payload) {
    var card = $('job-card');
    card.classList.remove('hidden');
    hide($('job-error'));
    hide($('job-result'));
    $('job-title').textContent = '正在提交…';
    $('job-stage').textContent = '—';
    $('job-meta').textContent = '';
    setStatusBadge('排队中', '');
    $('job-bar').style.width = '0%';

    api('/api/literature/analyze', { method: 'POST', body: payload }).then(function (data) {
      if (data.nothing_to_do) {
        $('job-title').textContent = '无需分析';
        $('job-stage').textContent = data.message;
        setStatusBadge('已完成', 'ok');
        toast('literature 中的 PDF 都已有分析结果');
        return;
      }
      state.jobId = data.job_id;
      $('job-title').textContent = '任务 ' + data.job_id;
      $('job-stage').textContent = '已加入队列（共 ' + data.count + ' 篇），开始处理…';
      startPolling(data.job_id);
    }).catch(function (err) {
      $('job-title').textContent = '提交失败';
      showError($('job-error'), err.message);
      setStatusBadge('失败', 'err');
    });
  }

  /* ---------------- 任务轮询 ---------------- */

  function setStatusBadge(text, kind) {
    var badge = $('job-status');
    badge.textContent = text;
    badge.className = 'badge' + (kind ? ' ' + kind : '');
  }

  function startPolling(jobId) {
    stopPolling();
    state.pollTicks = 0;
    pollJob(jobId);
    // 首次之后按服务端建议的间隔轮询（默认 30 秒）
    state.timer = setInterval(function () { pollJob(jobId); }, state.pollSeconds * 1000);
  }

  function stopPolling() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  function pollJob(jobId) {
    state.pollTicks += 1;
    api('/api/jobs/' + encodeURIComponent(jobId)).then(function (job) {
      if (!job) { return; }

      // 后端可以建议下次轮询秒数：任务刚启动给短间隔（用户立刻看到首条进度），
      // 跑了一阵改回长间隔（反代穿透下省流量）。收到 hint 就立即应用。
      if (typeof job.poll_hint === 'number' && job.poll_hint > 0 && job.poll_hint !== state.pollSeconds) {
        state.pollSeconds = job.poll_hint;
        if (state.timer) {
          clearInterval(state.timer);
          state.timer = setInterval(function () { pollJob(jobId); }, state.pollSeconds * 1000);
        }
      }

      renderJob(job);
      if (job.status === 'succeeded' || job.status === 'failed') {
        stopPolling();
        // 完成时取一次完整结果（只多这一次请求）
        api('/api/jobs/' + encodeURIComponent(jobId) + '?full=1').then(function (full) {
          if (full && full.result) { renderResult(full.result); }
        }).catch(function () { /* 结果拿不到不影响主流程 */ });
        loadFiles();
      }
    }).catch(function (err) {
      stopPolling();
      showError($('job-error'), err.message);
    });
  }

  function renderJob(job) {
    var stage = job.stage || '处理中';
    if (job.current && job.current !== '—') { stage += '（' + job.current + '）'; }
    $('job-stage').textContent = job.message ? job.message : stage;

    var total = job.total || 0;
    var done = job.done || 0;
    var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (job.status === 'succeeded' ? 100 : 8);
    $('job-bar').style.width = pct + '%';

    var parts = [];
    if (total > 0) { parts.push('进度 ' + done + '/' + total); }
    if (job.failed) { parts.push('失败 ' + job.failed); }
    if (job.elapsed) { parts.push('已用 ' + job.elapsed + ' 秒'); }
    if (job.status === 'running') { parts.push('每 ' + state.pollSeconds + ' 秒同步一次'); }
    $('job-meta').textContent = parts.join(' · ');

    if (job.status === 'queued') { setStatusBadge('排队中', ''); }
    else if (job.status === 'running') { setStatusBadge('进行中', ''); }
    else if (job.status === 'succeeded') { setStatusBadge('已完成', 'ok'); }
    else if (job.status === 'failed') { setStatusBadge('失败', 'err'); }

    if (job.error) {
      showError($('job-error'), job.error.message || '任务失败。');
    }
  }

  function renderResult(result) {
    var box = $('job-result');
    box.textContent = '';
    box.classList.remove('hidden');

    var head = document.createElement('p');
    head.className = 'small';
    head.textContent = '共 ' + result.total + ' 篇：成功 ' + (result.ok || []).length +
      '，跳过 ' + (result.skipped || []).length +
      '，失败 ' + (result.failed || []).length +
      '；AI 调用 ' + (result.calls || 0) + ' 次，耗时 ' + (result.elapsed_label || '—') + '。';
    box.appendChild(head);

    appendList(box, '已生成', result.ok, '成功');
    appendList(box, '已跳过（已有同名 Markdown）', result.skipped, '跳过');
    if (result.failed && result.failed.length) {
      appendList(box, '失败', (result.failed || []).map(function (item) {
        return item.file + ' — ' + item.message;
      }), '失败');
    }
    if (result.missing && result.missing.length) {
      appendList(box, '存在缺失分块（结果顶部已标注）', result.missing, '注意');
    }
  }

  function appendList(box, title, items, tag) {
    if (!items || !items.length) { return; }
    var p = document.createElement('p');
    p.className = 'small';
    p.textContent = title + '：';
    box.appendChild(p);
    var ul = document.createElement('ul');
    items.forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'small';
      li.textContent = item;
      ul.appendChild(li);
    });
    box.appendChild(ul);
    if (tag === '注意') { toast('部分分块未能分析，结果顶部已标注'); }
  }

  /* ---------------- 查看分析结果 ---------------- */

  function viewMarkdown(name) {
    $('viewer-title').textContent = name;
    $('viewer-content').textContent = '加载中…';
    $('viewer').classList.remove('hidden');
    api('/api/literature/content?name=' + encodeURIComponent(name)).then(function (data) {
      var text = data.content || '（空文件）';
      if (data.truncated) {
        text += '\n\n…… 内容过长，仅显示前 ' + data.chars + ' 个字符。完整内容请到 NAS 上打开该文件。';
      }
      $('viewer-content').textContent = text;
    }).catch(function (err) {
      $('viewer-content').textContent = err.message;
    });
  }

  $('viewer-close').addEventListener('click', function () { hide($('viewer')); });
  $('viewer').addEventListener('click', function (event) {
    if (event.target === $('viewer')) { hide($('viewer')); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { hide($('viewer')); }
  });

  /* ---------------- 生活记录 ---------------- */

  $('note-submit').addEventListener('click', function () {
    var input = $('note-input');
    var text = input.value.trim();
    if (!text) { toast('请先输入内容'); return; }

    var button = $('note-submit');
    button.disabled = true;
    button.textContent = '提交中…';

    api('/api/notes', { method: 'POST', body: { text: text } }).then(function (data) {
      input.value = '';
      renderNoteResult(data);
      loadNotes();
      toast('已记录为「' + data.label + '」');
    }).catch(function (err) {
      toast(err.message);
    }).finally(function () {
      button.disabled = false;
      button.textContent = '提交记录';
    });
  });

  function renderNoteResult(data) {
    var box = $('note-result');
    box.textContent = '';
    box.classList.remove('hidden');

    var head = document.createElement('div');
    head.className = 'note-head';
    var method = data.method === 'rule' ? '规则判定' : (data.method === 'model' ? '模型判定' : '未能判定，按随笔记录');
    head.textContent = '已识别为「' + data.label + '」（' + method + '），已追加到 life_notes/' + data.path;
    box.appendChild(head);

    if (data.amount) {
      var amount = document.createElement('div');
      amount.className = 'small';
      amount.textContent = '金额：' + (data.direction === 'in' ? '收入' : '支出') + ' ¥' + Number(data.amount).toFixed(2);
      box.appendChild(amount);
    }

    var pre = document.createElement('pre');
    pre.textContent = data.entry || '';
    box.appendChild(pre);
  }

  function loadNotes() {
    var preview = $('notes-preview');
    api('/api/notes/recent').then(function (data) {
      preview.textContent = data.content && data.content.trim()
        ? data.content
        : '（还没有任何记录。上面写一条试试。）';
    }).catch(function (err) {
      preview.textContent = err.message;
    });
  }

  function loadLog() {
    var preview = $('log-preview');
    preview.textContent = '加载中…';
    api('/api/notes/log').then(function (data) {
      preview.textContent = data.content && data.content.trim()
        ? data.content
        : '（还没有任务日志。完成一次分析或记录后会出现在这里。）';
    }).catch(function (err) {
      preview.textContent = err.message;
    });
  }

  $('refresh-notes').addEventListener('click', loadNotes);
  $('refresh-log').addEventListener('click', loadLog);

  /* ---------------- 启动 ---------------- */

  boot();
})();

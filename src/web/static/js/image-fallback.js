/* image-fallback.js — 图片加载失败提示 + 微信 wxgf(H.265) 图片的 ffmpeg 引导/安装
 *
 * 微信 wxgf 图片需要 ffmpeg 才能转成可显示的 JPEG（原图）；没有 ffmpeg 时
 * 后端会自动退回图片自带的小缩略图。本脚本负责：
 *   1) 裂图/415 时显示可读说明与操作按钮
 *   2) 检测 ffmpeg 是否就绪（GET /api/wxgf/status）
 *   3) 一键下载安装 ffmpeg 到 <程序目录>\tools\（POST /api/wxgf/install-ffmpeg, SSE 进度）
 *   4) 每 5 秒轮询一次：一旦检测到 ffmpeg，自动重试所有失败图片（无需刷新页面）
 */
(function () {
  "use strict";

  var state = { pending: null, watch: null, installing: false };

  function fetchStatus(force) {
    if (force) state.pending = null;
    if (state.pending) return state.pending;
    state.pending = fetch("/api/wxgf/status")
      .then(function (r) { return r.json(); })
      .catch(function () { return null; });
    return state.pending;
  }

  function btn(label, onclick, extraStyle) {
    return "<button class=\"btn\" style=\"padding:4px 10px;font-size:11px;" +
           (extraStyle || "") + "\" onclick=\"" + onclick + "\">" + label + "</button>";
  }

  function actionButtons(st, imgId) {
    var html = "<div style=\"margin-top:8px\">";
    if (!st || !st.installed) {
      html += btn("⬇ 一键安装 ffmpeg（约 100MB）", "ImageFallback.install(this)");
      html += " <a class=\"btn\" style=\"padding:4px 10px;font-size:11px;text-decoration:none;display:inline-block\" " +
              "href=\"https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip\" " +
              "target=\"_blank\" rel=\"noopener\">手动下载</a>";
    }
    html += " " + btn("📂 打开 tools 目录", "ImageFallback.openTools()");
    html += " " + btn("🔄 重试", "ImageFallback.retry(this)", "margin-left:6px");
    html = html.replace("onclick=\"ImageFallback.retry(this)\"",
                        "data-img=\"" + imgId + "\" onclick=\"ImageFallback.retry(this)\"") + "</div>";
    return html;
  }

  function renderBox(box, st, imgId) {
    var target = (st && st.targetPath) ? st.targetPath : "(程序目录)\\tools\\ffmpeg.exe";
    var head, detail;
    if (!st || !st.installed) {
      head = "该图片是微信 wxgf(H.265) 格式，显示原图需要 ffmpeg";
      detail = "① 点「一键安装 ffmpeg」自动下载并放到 <code>" + target + "</code><br>" +
               "② 或手动下载后把 <code>bin\\ffmpeg.exe</code> 放进同一目录<br>" +
               "③ 安装完成后本页会<b>自动转换并显示原图</b>（无需重启程序）<br>" +
               "<span style=\"color:#6e7681\">没有 ffmpeg 时，只会显示图片自带的小缩略图。</span>";
    } else {
      head = "图片解码失败";
      detail = "已检测到 ffmpeg：" + (st.ffmpeg || "") + "<br>" +
               "请在微信里打开一次这张图片（让它下载到本地），再点「重试」。";
    }
    box.innerHTML = "<div style=\"color:#d29922;font-size:12px;font-weight:600;margin-bottom:6px\">" + head + "</div>" +
      "<div style=\"color:#8b949e;font-size:11px;line-height:1.9;text-align:left\">" + detail + "</div>" +
      "<div class=\"wxgf-progress\" style=\"color:#58a6ff;font-size:11px;margin-top:6px;display:none\"></div>" +
      actionButtons(st, imgId);
  }

  function retryEl(img) {
    if (!img) return;
    var box = img.nextElementSibling;
    if (box && box.classList && box.classList.contains("img-fallback")) box.style.display = "none";
    img.style.display = "";
    img.removeAttribute("data-failed");
    var base = img.src.split("&_r=")[0];
    img.src = base + "&_r=" + Date.now();
  }

  function retryAllFailed() {
    var failed = document.querySelectorAll("img.bubble-image[data-failed=\"1\"]");
    for (var i = 0; i < failed.length; i++) retryEl(failed[i]);
  }

  function startAutoWatch() {
    if (state.watch) return;
    var tries = 0;
    state.watch = setInterval(function () {
      tries += 1;
      if (tries > 120) {
        clearInterval(state.watch);
        state.watch = null;
        return;
      }
      fetchStatus(true).then(function (st) {
        if (!st || !st.installed) return;
        clearInterval(state.watch);
        state.watch = null;
        retryAllFailed();
      });
    }, 5000);
  }

  function setProgress(html) {
    var nodes = document.querySelectorAll(".wxgf-progress");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].style.display = "block";
      nodes[i].innerHTML = html;
    }
  }

  function install(url) {
    if (state.installing) return;
    state.installing = true;
    setProgress("正在启动下载…");
    fetch("/api/wxgf/install-ffmpeg", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url || "" }),
    }).then(function (resp) {
      if (!resp.body) {
        state.installing = false;
        setProgress("浏览器不支持流式进度，请稍候或手动安装");
        return;
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder("utf-8");
      var buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            state.installing = false;
            return;
          }
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(function (block) {
            var ev = "message", data = "";
            block.split("\n").forEach(function (line) {
              if (line.indexOf("event: ") === 0) ev = line.slice(7).trim();
              else if (line.indexOf("data: ") === 0) data += line.slice(6);
            });
            if (!data) return;
            var obj = null;
            try { obj = JSON.parse(data); } catch (e) { return; }
            if (ev === "progress") setProgress(obj.detail || "");
            else if (ev === "done") {
              state.installing = false;
              state.pending = null;
              setProgress("✅ ffmpeg 安装完成，正在自动转换图片…");
              setTimeout(function () { fetchStatus(true).then(retryAllFailed); }, 800);
            } else if (ev === "error") {
              state.installing = false;
              setProgress("❌ " + (obj.message || "安装失败") +
                          "　可手动下载 ffmpeg 后放入 tools 目录");
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      state.installing = false;
      setProgress("❌ 请求失败: " + e);
    });
  }

  var ImageFallback = {
    handle: function (img) {
      if (!img) return;
      img.dataset.failed = "1";
      var box = img.nextElementSibling;
      img.style.display = "none";
      if (!box || !box.classList || !box.classList.contains("img-fallback")) return;
      box.style.display = "block";
      box.innerHTML = "<div style=\"color:#8b949e;font-size:11px\">正在检查图片解码环境…</div>";
      var imgId = img.id || "";
      fetchStatus().then(function (st) {
        renderBox(box, st, imgId);
        if (!st || !st.installed) startAutoWatch();
      });
    },

    retry: function (el) {
      var id = el && el.getAttribute ? el.getAttribute("data-img") : "";
      var img = id ? document.getElementById(id) : null;
      fetchStatus(true).then(function () { retryEl(img); });
    },

    install: function () { install(""); },

    openTools: function () {
      fetch("/api/wxgf/open-tools", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.error) alert("打开目录失败: " + (d.message || d.error));
          else setProgress("已打开目录：" + (d.toolsDir || ""));
          startAutoWatch();
        })
        .catch(function (e) { alert("请求失败: " + e); });
    },
  };

  window.ImageFallback = ImageFallback;
})();

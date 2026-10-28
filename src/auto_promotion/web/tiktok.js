"use strict";

function tiktokErrorMessage(value) {
  return (
    {
      TikTokPermissionDenied:
        "账号缺少所需权限，请重新授权发布、视频读取和评论管理权限。",
      TikTokAuthorizationRequired: "TikTok 授权已过期或被撤销，请重新连接。",
      TikTokIdentityMismatch: "授权账号与原绑定身份不一致，请核对账号。",
      TikTokVerifiedMediaPrefixRequired:
        "素材地址不在已登记的域名前缀内，请先完成 TikTok 域名验证和本机配置。",
      TikTokOwnedTargetRequired: "请选择此账号自己的发布目标。",
      TikTokOwnedVideoRequired: "该内容未能核实为此账号所有。",
      TikTokCommentUnavailable: "未找到此内容下的目标评论。",
      TikTokCanOnlyDeleteOwnComment:
        "删除接口只支持自己发表的评论；其他评论可选择隐藏。",
      TikTokRateLimited: "TikTok 请求受到限流，请在冷却结束后重试读取。",
      TikTokTransportUnavailable:
        "暂时无法连接 TikTok；已提交的项目请只读核对结果。",
      TikTokPublicHTTPSRequired: "请填写可公开访问的有效 HTTPS 地址。",
      TikTokMediaAndOptionsRequired: "请添加媒体并填写 TikTok 发布选项。",
      TikTokCannotMixMedia: "一次发布需要一个视频，或一组图片。",
      TikTokPhotoOptionsInvalid: "请检查图文可见范围、标题长度和封面序号。",
      TikTokCaptionTooLong: "文案超过 TikTok 字符限制。",
      TikTokCommentLengthInvalid:
        "评论不能为空，且须符合 1,200 UTF-8 字节上限。",
      TikTokUnsupportedMediaType:
        "视频支持 MP4、MOV、WebM；图文支持 JPEG、WebP。",
      TikTokVideoPublicOnly: "此视频接口只支持公开发布。",
      TikTokInteractionDisabled: "账号设置已关闭此互动方式，请调整发布选项。",
    }[value] || value
  );
}

function tiktokItemSummary(item, result) {
  if (item.platform !== "tiktok") return "";
  const options = item.tiktok_options;
  const rows =
    item.action === "publish" && options
      ? [
          options.privacy_level || "公开视频",
          options.is_brand_organic ? "推广自有品牌" : "",
          options.is_branded_content ? "付费品牌合作" : "",
          options.is_ai_generated || item.media.some((m) => m.is_ai_generated)
            ? "AI 生成标记"
            : "",
          options.disable_comment ? "评论关闭" : "评论开放",
          `图文封面：第 ${(options.photo_cover_index || 0) + 1} 张`,
          `视频封面：${options.thumbnail_offset || 0} 毫秒`,
        ].filter(Boolean)
      : [
          `自有内容 ID：${item.tiktok_video_id || ""}`,
          item.tiktok_comment_id ? `评论 ID：${item.tiktok_comment_id}` : "",
        ].filter(Boolean);
  const stages = {
    ACCEPTED: "平台已接收，等待核验",
    PROCESSING_DOWNLOAD: "平台正在读取素材",
    PUBLISH_COMPLETE: "平台发布完成",
    FAILED: "平台发布失败",
    SEND_TO_USER_INBOX: "已进入 TikTok 草稿通知",
  };
  return `<p class="help">${rows.map(esc).join(" · ")}</p>${result.platform_status ? `<p class="help">${esc(stages[result.platform_status] || result.platform_status)}</p>` : ""}`;
}

async function tiktokSetup() {
  const config = await api("/tiktok/app");
  openPanel(
    "连接 TikTok",
    `<p class="help">使用 TikTok Accounts API 接入自己的账号。先保存开发者应用，再逐个授权。应用密钥保存在本机私密配置中。</p>
    <details ${config.configured ? "" : "open"}><summary>开发者应用设置${config.configured ? "（已配置）" : ""}</summary>
    <form id="tiktok-app-form"><div class="field-grid">
    <div><label>App ID<input name="client_id" value="${esc(config.client_id || "")}" required autocomplete="off"></label></div>
    <div><label>App secret<input name="client_secret" type="password" required autocomplete="new-password"></label></div>
    <div class="full"><label>HTTPS 授权回调地址<input name="redirect_uri" type="url" value="${esc(config.redirect_uri || "")}" placeholder="https://your-domain.example/oauth/tiktok/" required></label><p class="help">与开发者后台一致，以 / 结尾。可部署独立回调接收器；也可将授权完成后的完整地址粘贴到下方。</p></div>
    <div class="full"><label>账号持有人授权 URL<input name="authorization_url" type="url" value="${esc(config.authorization_url || "")}" required></label><p class="help">从 TikTok for Business 的应用详情复制，权限需包含本次使用的发布、视频读取和评论管理权限。</p></div>
    <div class="full"><label>已在 TikTok 验证的素材 URL 前缀<textarea name="media_url_prefixes" placeholder="https://cdn.your-domain.example/tiktok/">${esc((config.media_url_prefixes || []).join("\n"))}</textarea></label><p class="help">每行一个，以 / 结尾。此处登记不会代替 TikTok 的域名所有权验证。</p></div>
    </div><div class="form-actions"><button type="submit">保存应用配置</button></div></form></details>
    <form id="tiktok-authorize-form"><h3>授权一个账号</h3><div class="field-grid"><div><label>TikTok 用户名<input name="username" placeholder="不含 @" required></label></div><div><label>本机账号标签<input name="backend_label" placeholder="tiktok-brand" required></label></div></div><div class="form-actions"><button type="submit" ${config.configured ? "" : "disabled"}>生成授权链接</button></div></form><div id="tiktok-authorization-link"></div>
    <form id="tiktok-complete-form"><label>授权完成后的完整回调地址<textarea name="callback_url" required autocomplete="off" placeholder="仅在未部署自动回调时使用"></textarea></label><div class="form-actions"><button type="submit">完成账号连接</button><button type="button" class="secondary" id="tiktok-refresh-accounts">刷新账号列表</button></div></form>`,
  );
  $("#tiktok-app-form").onsubmit = (event) => {
    event.preventDefault();
    const form = event.target,
      data = Object.fromEntries(new FormData(form));
    data.media_url_prefixes = data.media_url_prefixes
      .split(/\r?\n/)
      .map((x) => x.trim())
      .filter(Boolean);
    busy(form.querySelector("button"), async () => {
      await api("/tiktok/app", "PUT", data);
      form.querySelector('[name="client_secret"]').value = "";
      await tiktokSetup();
      toast("TikTok 应用配置已保存");
    });
  };
  $("#tiktok-authorize-form").onsubmit = (event) => {
    event.preventDefault();
    busy(event.target.querySelector("button"), async () => {
      const result = await api(
        "/tiktok/authorize",
        "POST",
        Object.fromEntries(new FormData(event.target)),
      );
      $("#tiktok-authorization-link").innerHTML =
        `<p><a href="${esc(result.authorization_url)}" target="_blank" rel="noreferrer">打开 TikTok 并授权账号</a></p><p class="help">链接有效至 ${esc(fmt(result.expires_at))}。完成授权后，自动回调会登记账号；未部署接收器时，将地址栏中的完整回调地址粘贴到下方。</p>`;
    });
  };
  $("#tiktok-complete-form").onsubmit = (event) => {
    event.preventDefault();
    busy(event.target.querySelector("button"), async () => {
      const result = await api(
        "/tiktok/authorize/complete",
        "POST",
        Object.fromEntries(new FormData(event.target)),
      );
      event.target.reset();
      await refresh();
      $("#panel").close();
      renderAccounts();
      toast(`TikTok @${result.username} 已连接；自有目标 ID：${result.target}`);
    });
  };
  $("#tiktok-refresh-accounts").onclick = async () => {
    await refresh();
    $("#panel").close();
    renderAccounts();
  };
  if (config.configured) {
    $("#panel-content").insertAdjacentHTML(
      "beforeend",
      `<details><summary>发布和评论回调</summary><p class="help">先部署独立回调接收器，再注册通知。注册地址为授权回调同域名下的 /webhooks/tiktok；此操作会更新 TikTok 应用的回调设置。</p><form id="tiktok-webhook-form"><label>通知类型<select name="event_type"><option value="VIDEO">发布结果</option><option value="COMMENT">评论更新</option></select></label><div class="form-actions"><button type="submit">注册回调通知</button></div></form></details>`,
    );
    $("#tiktok-webhook-form").onsubmit = (event) => {
      event.preventDefault();
      busy(event.target.querySelector("button"), async () => {
        const result = await api(
          "/tiktok/webhooks",
          "POST",
          Object.fromEntries(new FormData(event.target)),
        );
        toast(`${result.event_type} 回调已注册：${result.callback_url}`);
      });
    };
  }
}

async function tiktokLibrary(accountId) {
  const account = state.accounts.find((a) => a.id === accountId);
  openPanel(
    "TikTok 内容与评论",
    `<p>${esc(account.label)}</p><p class="help">读取自有内容和评论，导入内容池后可按现有流程准备互动任务。这里的操作只读取数据。</p><div class="actions"><button id="tiktok-read-videos">读取自有内容</button><button class="secondary" id="tiktok-insights">读取账号数据</button></div><div id="tiktok-insight-result"></div><div id="tiktok-videos"></div><form id="tiktok-comments-form"><label>自有视频／图文 ID<input name="video_id" required pattern="[0-9]{1,30}"></label><div class="form-actions"><button type="submit">读取并导入评论</button></div></form><div id="tiktok-comments"></div>`,
  );
  let videoCursor = 0,
    commentCursor = 0,
    currentVideo = "";
  const videoButton = $("#tiktok-read-videos");
  videoButton.onclick = () =>
    busy(videoButton, async () => {
      const data = await api(`/tiktok/accounts/${accountId}/videos`, "POST", {
        cursor: videoCursor,
        limit: 20,
      });
      videoCursor = data.next_cursor;
      $("#tiktok-videos").insertAdjacentHTML(
        "beforeend",
        (data.targets || [])
          .map(
            (v) =>
              `<article class="item-preview"><strong>${esc(v.caption || "无文案")}</strong><p>内容 ID：${esc(v.item_id)}</p><button type="button" class="secondary compact" data-tiktok-video="${esc(v.item_id)}">选择评论目标</button></article>`,
          )
          .join("") || '<p class="muted">此页未返回内容。</p>',
      );
      videoButton.dataset.more = data.has_more ? "1" : "0";
      $("#tiktok-videos")
        .querySelectorAll("[data-tiktok-video]")
        .forEach(
          (b) =>
            (b.onclick = () => {
              $("#tiktok-comments-form input").value = b.dataset.tiktokVideo;
            }),
        );
    }).then(() => {
      videoButton.disabled = videoButton.dataset.more === "0";
      if (!videoButton.disabled) videoButton.textContent = "读取下一页内容";
    });
  $("#tiktok-insights").onclick = (event) =>
    busy(event.target, async () => {
      const data = await api(
        `/tiktok/accounts/${accountId}/insights`,
        "POST",
        {},
      );
      const labels = {
        username: "用户名",
        display_name: "显示名称",
        followers_count: "粉丝",
        following_count: "关注",
        total_likes: "获赞",
        videos_count: "内容数",
      };
      $("#tiktok-insight-result").innerHTML = `<dl>${Object.entries(
        data.insights || {},
      )
        .map(
          ([k, v]) =>
            `<dt>${esc(labels[k] || k)}</dt><dd>${esc(v ?? "未返回")}</dd>`,
        )
        .join(
          "",
        )}</dl><p class="help">仅展示接口实际返回的数据；未返回的指标保持未知。</p>`;
    });
  $("#tiktok-comments-form").onsubmit = (event) => {
    event.preventDefault();
    const button = event.target.querySelector("button"),
      video = event.target.elements.video_id.value.trim();
    if (video !== currentVideo) {
      commentCursor = 0;
      currentVideo = video;
      $("#tiktok-comments").innerHTML = "";
    }
    busy(button, async () => {
      const data = await api(`/tiktok/accounts/${accountId}/comments`, "POST", {
        video_id: video,
        cursor: commentCursor,
        limit: 30,
        import_to_pool: true,
      });
      commentCursor = data.next_cursor;
      $("#tiktok-comments").insertAdjacentHTML(
        "beforeend",
        (data.content_items || [])
          .map(
            (c) =>
              `<article class="item-preview"><p>${esc(c.context)}</p><p class="help">评论 ID：${esc(c.tiktok_comment_id)}</p></article>`,
          )
          .join("") || '<p class="muted">此页未返回可导入的评论。</p>',
      );
      button.dataset.more = data.has_more ? "1" : "0";
      toast(
        `评论已导入内容池；新增 ${data.imported.imported} 条，更新 ${data.imported.updated} 条`,
      );
    }).then(() => {
      button.textContent =
        button.dataset.more === "1" ? "读取并导入下一页评论" : "重新读取评论";
      if (button.dataset.more === "0") commentCursor = 0;
    });
  };
}

function tiktokItemFields(item) {
  const account = state.accounts.find((a) => a.id === item.account_id);
  if (account?.platform === "tiktok" && account.tiktok_transport === "tabbit")
    return `<div class="full"><p class="help">Tabbit 视频留言：目标填写完整视频链接并核对视频 ID。顶层留言不填父评论；回复须填写同视频的原生父评论 ID。访问受阻或关联不可验证时停止。</p><label>目标视频 ID<input name="tiktok_video_id" inputmode="numeric" pattern="[1-9][0-9]{0,29}" value="${esc(item.tiktok_video_id || "")}" required></label><label>父评论 ID（仅回复必填）<input name="tiktok_comment_id" inputmode="numeric" pattern="[1-9][0-9]{0,29}" value="${esc(item.tiktok_comment_id || "")}"></label></div>`;
  if (
    state.accounts.find((a) => a.id === item.account_id)?.platform !== "tiktok"
  )
    return "";
  if (item.action !== "publish")
    return `<div class="full"><p class="help">评论操作仅限此账号自有视频；删除仅支持自己发表的评论。</p><label>自有视频／图文 ID<input name="tiktok_video_id" inputmode="numeric" pattern="[0-9]{1,30}" value="${esc(item.tiktok_video_id || "")}" required></label><label>评论 ID（回复或管理时必填）<input name="tiktok_comment_id" inputmode="numeric" pattern="[0-9]{1,30}" value="${esc(item.tiktok_comment_id || "")}"></label></div>`;
  const options = item.tiktok_options || {
    disable_comment: true,
    disable_duet: true,
    disable_stitch: true,
  };
  return `<div class="full"><p class="help">视频将公开发布。图文按所选可见范围发布；私密图文可能无法取得公开回执。素材地址必须来自已在 TikTok 验证的域名。</p><label>图文可见范围<select name="tiktok_privacy_level"><option value="">请选择（图文必填）</option>${[
    ["PUBLIC_TO_EVERYONE", "所有人"],
    ["MUTUAL_FOLLOW_FRIENDS", "互相关注的朋友"],
    ["FOLLOWER_OF_CREATOR", "关注者"],
    ["SELF_ONLY", "仅自己"],
  ]
    .map(
      ([v, t]) =>
        `<option value="${v}" ${options.privacy_level === v ? "selected" : ""}>${t}</option>`,
    )
    .join("")}</select></label><div class="field-grid">${[
    ["disable_comment", "关闭评论"],
    ["disable_duet", "关闭合拍（视频）"],
    ["disable_stitch", "关闭拼接（视频）"],
    ["is_brand_organic", "推广自己的品牌"],
    ["is_branded_content", "与其他品牌的付费合作"],
    ["is_ai_generated", "AI 生成内容（视频）"],
    ["auto_add_music", "自动添加推荐音乐（图文）"],
  ]
    .map(
      ([k, t]) =>
        `<label class="checkbox-label"><input type="checkbox" data-tiktok-option="${k}" ${options[k] ? "checked" : ""}>${t}</label>`,
    )
    .join(
      "",
    )}</div><label>视频封面帧（毫秒）<input name="tiktok_thumbnail_offset" type="number" min="0" max="600000" value="${options.thumbnail_offset || 0}"></label><label>图文封面序号（从 1 开始）<input name="tiktok_photo_cover" type="number" min="1" max="10" value="${(options.photo_cover_index || 0) + 1}"></label></div>`;
}

function collectTikTokFields(row, item) {
  if (
    state.accounts.find((a) => a.id === item.account_id)?.platform !== "tiktok"
  )
    return;
  item.tiktok_video_id =
    row.querySelector('[name="tiktok_video_id"]')?.value.trim() || "";
  item.tiktok_comment_id =
    row.querySelector('[name="tiktok_comment_id"]')?.value.trim() || "";
  item.tiktok_options = null;
  if (item.action !== "publish") return;
  if (!row.querySelector('[name="tiktok_privacy_level"]')) return;
  item.tiktok_options = {
    privacy_level: row.querySelector('[name="tiktok_privacy_level"]').value,
    thumbnail_offset: Number(
      row.querySelector('[name="tiktok_thumbnail_offset"]').value,
    ),
    photo_cover_index:
      Number(row.querySelector('[name="tiktok_photo_cover"]').value) - 1,
  };
  row
    .querySelectorAll("[data-tiktok-option]")
    .forEach(
      (el) => (item.tiktok_options[el.dataset.tiktokOption] = el.checked),
    );
}

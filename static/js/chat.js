(function () {
  // ========== GLOBAL VARIABLES ==========
  let CHAT_TYPE = "global";
  let CHAT_USER = null;
  let GROUP_ID = null;
  if (typeof window.CHAT_TYPE !== "undefined") CHAT_TYPE = window.CHAT_TYPE;
  if (typeof window.CHAT_USER !== "undefined") CHAT_USER = window.CHAT_USER;
  if (typeof window.GROUP_ID !== "undefined") GROUP_ID = window.GROUP_ID;

  let eventSource = null;
  let messagesContainer, messageForm, messageInput, fileInput, sendBtn;
  let currentUser = "",
    currentDisplay = "",
    currentAvatar = "";
  let reconnectAttempts = 0;
  const MAX_RECONNECT_DELAY = 15000;
  let hamburgerBtn, sidebar, overlay, themeToggle;

  // Reply state
  let activeReply = null;
  let replyPreviewDiv = null;
  let replyCancelBtn = null;

  // Context menu
  let contextMenu = null;

  // User data cache
  const usersData = window.USERS_DATA || {};

  // Group data
  let groupData = window.GROUP_DATA || null;

  // ========== PAGE DETECTION ==========
  const isChatPage = !!document.getElementById("chatMessages");
  const isProfilePage = !!document.querySelector(".profile-view-container");
  const isUsersPage = !!document.querySelector(".users-grid");

  // ========== INITIALIZATION ==========
  document.addEventListener("DOMContentLoaded", () => {
    messagesContainer = document.getElementById("chatMessages");
    messageForm = document.getElementById("messageForm");
    messageInput = document.getElementById("messageInput");
    fileInput = document.getElementById("fileInput");
    sendBtn = document.getElementById("sendBtn");

    if (window.CURRENT_USER_DATA) {
      currentUser = window.CURRENT_USER_DATA.username;
      currentDisplay = window.CURRENT_USER_DATA.display_name;
      currentAvatar = window.CURRENT_USER_DATA.avatar;
    } else {
      currentUser = document.body.getAttribute("data-user") || "";
      currentDisplay =
        document.body.getAttribute("data-display") || currentUser;
    }

    initTheme();
    initSidebar();
    initContextMenu();
    initReplyPreview();

    if (isChatPage) {
      // Wait for video player script to be available
      if (typeof createRetroVideoPlayer === "undefined") {
        console.warn("Video player not ready, retrying...");
        setTimeout(() => enhanceExistingMessages(), 150);
      } else {
        enhanceExistingMessages();
      }
      initSSE();
      setupEventListeners();
      if (messageInput) {
        messageInput.focus();
        initAutoExpand();
      }
      scrollToBottom();
    }

    initAvatarPreview();
    initProfileLinks();

    // Group-specific admin buttons
    if (CHAT_TYPE === "group") {
      initGroupAdminControls();
    }

    if (isChatPage) {
      let viewportHeight = window.innerHeight;
      window.addEventListener("resize", () => {
        const keyboardOpen = window.innerHeight < viewportHeight - 150;
        setTimeout(() => {
          scrollToBottom();
          if (keyboardOpen && messageInput)
            messageInput.scrollIntoView({
              behavior: "smooth",
              block: "center",
            });
        }, 100);
        viewportHeight = window.innerHeight;
      });
    }
  });

  // ========== AUTO-EXPAND TEXTAREA ==========
  function initAutoExpand() {
    if (!messageInput || messageInput.tagName !== "TEXTAREA") return;
    const resize = () => {
      messageInput.style.height = "auto";
      messageInput.style.height = messageInput.scrollHeight + "px";
    };
    messageInput.addEventListener("input", resize);
    setTimeout(resize, 0);
    window.addEventListener("resize", resize);
  }

  // ========== CONTEXT MENU ==========
  function initContextMenu() {
    contextMenu = document.createElement("div");
    contextMenu.id = "messageContextMenu";
    contextMenu.className = "context-menu";
    contextMenu.style.display = "none";
    document.body.appendChild(contextMenu);

    document.addEventListener("click", () => {
      if (contextMenu) contextMenu.style.display = "none";
    });
  }

  function showContextMenu(e, messageData) {
    e.preventDefault();
    e.stopPropagation();

    contextMenu.innerHTML = "";

    const copyBtn = document.createElement("button");
    copyBtn.textContent = "📋 Copy";
    copyBtn.onclick = (ev) => {
      ev.stopPropagation();
      navigator.clipboard.writeText(messageData.text || "");
      contextMenu.style.display = "none";
    };
    contextMenu.appendChild(copyBtn);

    const replyBtn = document.createElement("button");
    replyBtn.textContent = "↩️ Reply";
    replyBtn.onclick = (ev) => {
      ev.stopPropagation();
      setReplyPreview(messageData);
      contextMenu.style.display = "none";
    };
    contextMenu.appendChild(replyBtn);

    let canDelete = messageData.from === currentUser;
    if (
      CHAT_TYPE === "group" &&
      groupData &&
      groupData.creator === currentUser
    ) {
      canDelete = true;
    }
    if (canDelete) {
      const deleteBtn = document.createElement("button");
      deleteBtn.textContent = "🗑️ Delete";
      deleteBtn.className = "delete-option";
      deleteBtn.onclick = async (ev) => {
        ev.stopPropagation();
        if (confirm("Delete this message?")) {
          await deleteMessage(messageData.id);
        }
        contextMenu.style.display = "none";
      };
      contextMenu.appendChild(deleteBtn);
    }

    // Position the menu, clamped so it never overflows the viewport
    // (folded in from static/js/overflow.js, which was never loaded).
    contextMenu.style.display = "block";
    contextMenu.style.visibility = "hidden";
    contextMenu.style.left = "0px";
    contextMenu.style.top = "0px";
    contextMenu.offsetHeight; // force reflow for accurate dimensions

    const menuWidth = contextMenu.offsetWidth;
    const menuHeight = contextMenu.offsetHeight;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let left = e.clientX;
    let top = e.clientY;

    if (left + menuWidth > viewportWidth) left = left - menuWidth;
    if (top + menuHeight > viewportHeight) top = top - menuHeight;

    left = Math.max(5, Math.min(left, viewportWidth - menuWidth - 5));
    top = Math.max(5, Math.min(top, viewportHeight - menuHeight - 5));

    contextMenu.style.left = `${left}px`;
    contextMenu.style.top = `${top}px`;
    contextMenu.style.visibility = "visible";
  }

  // ========== DELETE MESSAGE ==========
  async function deleteMessage(messageId) {
    try {
      let chatId = null;
      if (CHAT_TYPE === "dm") {
        const me = currentUser;
        const other = CHAT_USER;
        chatId = me < other ? `${me}__${other}` : `${other}__${me}`;
      } else if (CHAT_TYPE === "group") {
        chatId = GROUP_ID;
      }

      const resp = await fetch("/delete_message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message_id: messageId,
          chat_type: CHAT_TYPE,
          chat_id: chatId,
        }),
      });

      if (resp.ok) {
        const msgElement = document.querySelector(
          `.message[data-message-id="${messageId}"]`,
        );
        if (msgElement) msgElement.remove();
      } else {
        const error = await resp.json();
        alert(error.error || "Failed to delete");
      }
    } catch (err) {
      console.error(err);
      alert("Network error");
    }
  }

  // ========== REPLY PREVIEW ==========
  function initReplyPreview() {
    const footer = document.querySelector(".chat-footer");
    if (!footer) return;

    replyPreviewDiv = document.createElement("div");
    replyPreviewDiv.id = "replyPreview";
    replyPreviewDiv.className = "reply-preview";
    replyPreviewDiv.style.display = "none";

    replyCancelBtn = document.createElement("button");
    replyCancelBtn.textContent = "✕";
    replyCancelBtn.className = "reply-cancel";
    replyCancelBtn.onclick = clearReplyPreview;

    replyPreviewDiv.appendChild(replyCancelBtn);
    footer.insertBefore(replyPreviewDiv, footer.firstChild);
  }

  function setReplyPreview(messageData) {
    activeReply = messageData;
    if (!replyPreviewDiv) return;

    const senderName =
      messageData.from === currentUser
        ? "You"
        : getUserInfo(messageData.from).display_name;
    let content = `<strong>Replying to ${escapeHtml(senderName)}</strong><br>`;
    if (messageData.text) {
      content += `<div class="reply-text">${escapeHtml(
        messageData.text.substring(0, 150),
      )}${messageData.text.length > 150 ? "..." : ""}</div>`;
    }
    if (messageData.file) {
      const fileName = messageData.file.includes("_")
        ? messageData.file.split("_").slice(1).join("_")
        : messageData.file;
      const fileIcon = isVideoFile(messageData.file) ? "🎬" : "📎";
      content += `<div class="reply-file">${fileIcon} ${escapeHtml(fileName)}</div>`;
    }

    replyPreviewDiv.innerHTML = content;
    replyPreviewDiv.appendChild(replyCancelBtn);
    replyPreviewDiv.style.display = "flex";

    if (messageInput) messageInput.focus();
  }

  function clearReplyPreview() {
    activeReply = null;
    if (replyPreviewDiv) {
      replyPreviewDiv.style.display = "none";
      replyPreviewDiv.innerHTML = "";
      replyPreviewDiv.appendChild(replyCancelBtn);
    }
  }

  // ========== UTILITIES ==========
  function scrollToBottom() {
    if (!messagesContainer) return;
    // .chat-messages uses flex-direction: column-reverse, which reverses
    // the scroll axis: scrollTop === 0 shows the newest message.
    // (The old code set scrollTop = scrollHeight, jumping to the OLDEST
    // message in spec-compliant browsers.)
    messagesContainer.scrollTop = 0;
  }

  function formatTime(timestamp) {
    if (!timestamp) return "";
    const d = new Date(timestamp);
    const now = new Date();
    const hhmm = `${d.getHours().toString().padStart(2, "0")}:${d
      .getMinutes()
      .toString()
      .padStart(2, "0")}`;
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) return hhmm;
    const mmdd = `${(d.getMonth() + 1).toString().padStart(2, "0")}/${d
      .getDate()
      .toString()
      .padStart(2, "0")}`;
    return `${mmdd} ${hhmm}`;
  }

  function isImage(filename) {
    return /\.(png|jpg|jpeg|gif|bmp|webp)$/i.test(filename);
  }

  function isVideoFile(filename) {
    return /\.(mp4|webm|ogg|mov|avi|mkv|wmv|flv|m4v)$/i.test(filename);
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/[&<>"']/g, (m) => {
      switch (m) {
        case "&":
          return "&amp;";
        case "<":
          return "&lt;";
        case ">":
          return "&gt;";
        case '"':
          return "&quot;";
        case "'":
          return "&#39;";
        default:
          return m;
      }
    });
  }

  function getUserInfo(username) {
    if (username === currentUser)
      return { display_name: currentDisplay, avatar: currentAvatar };
    return usersData[username] || { display_name: username, avatar: null };
  }

  function renderAvatar(username) {
    const user = getUserInfo(username);
    const safeName = escapeHtml(username);
    if (user.avatar) {
      return `<img src="/avatars/${encodeURIComponent(
        user.avatar,
      )}" class="avatar-img" alt="${safeName}">`;
    }
    return `<div class="avatar-default">${safeName[0].toUpperCase()}</div>`;
  }

  // ========== CHAT FUNCTIONS ==========
  function addMessage(msg, isOwn, prepend = true) {
    if (!messagesContainer) return;
    if (document.querySelector(`.message[data-message-id="${msg.id}"]`)) return;

    const div = document.createElement("div");
    div.className = `message ${isOwn ? "sent" : "received"}`;
    div.setAttribute("data-from", msg.from);
    div.setAttribute("data-timestamp", msg.timestamp);
    div.setAttribute("data-message-id", msg.id);

    let avatarHtml = "";
    if (!isOwn) {
      avatarHtml = `<div class="avatar" data-username="${escapeHtml(
        msg.from,
      )}">${renderAvatar(msg.from)}</div>`;
    }

    let fileHtml = "";
    if (msg.file) {
      const safeFile = encodeURIComponent(msg.file);
      if (isImage(msg.file)) {
        fileHtml = `<div class="attachment"><img src="/uploads/${safeFile}" alt="media" loading="lazy" onclick="window.open(this.src)"></div>`;
      } else if (isVideoFile(msg.file)) {
        fileHtml = `<div class="attachment" data-video-src="/uploads/${safeFile}"></div>`;
      } else {
        const name = msg.file.includes("_")
          ? msg.file.split("_").slice(1).join("_")
          : msg.file;
        fileHtml = `<div class="attachment"><a href="/uploads/${safeFile}" target="_blank">📎 ${escapeHtml(
          name,
        )}</a></div>`;
      }
    }

    let replyHtml = "";
    if (msg.reply) {
      const replySender =
        msg.reply.from === currentUser
          ? "You"
          : getUserInfo(msg.reply.from).display_name;
      replyHtml = `
        <div class="message-quote">
          <div class="quote-header">↩️ Replying to ${escapeHtml(replySender)}</div>
          <div class="quote-text">${escapeHtml(msg.reply.text || "")}</div>
          ${
            msg.reply.file
              ? `<div class="quote-file">📎 ${escapeHtml(
                  msg.reply.file.split("_").pop() || msg.reply.file,
                )}</div>`
              : ""
          }
        </div>
      `;
    }

    const senderUser = getUserInfo(msg.from);
    const senderDisplay = isOwn ? currentDisplay : senderUser.display_name;
    const senderHtml = `<div class="sender">${
      isOwn
        ? escapeHtml(senderDisplay)
        : `<a href="#" class="profile-link" data-username="${escapeHtml(
            msg.from,
          )}">${escapeHtml(senderDisplay)}</a>`
    }</div>`;

    const bubbleContent = `
      ${senderHtml}
      ${replyHtml}
      <div class="text">${escapeHtml(msg.text || "")}</div>
      ${fileHtml}
      <div class="time">${formatTime(msg.timestamp)}</div>
    `;

    div.innerHTML = `${avatarHtml}<div class="bubble">${bubbleContent}</div>`;

    const bubbleElement = div.querySelector(".bubble");
    bubbleElement.addEventListener("click", (e) => {
      e.stopPropagation();
      if (
        e.target.tagName === "A" ||
        e.target.tagName === "IMG" ||
        e.target.tagName === "VIDEO" ||
        e.target.closest(".retro-video-player")
      )
        return;
      showContextMenu(e, {
        id: msg.id,
        from: msg.from,
        text: msg.text,
        file: msg.file,
        timestamp: msg.timestamp,
      });
    });

    if (prepend) {
      messagesContainer.insertBefore(div, messagesContainer.firstChild);
    } else {
      messagesContainer.appendChild(div);
    }

    // Initialize retro video players
    const videoPlaceholders = div.querySelectorAll(
      ".attachment[data-video-src]",
    );
    videoPlaceholders.forEach((placeholder) => {
      const videoSrc = placeholder.dataset.videoSrc;
      if (typeof createRetroVideoPlayer === "function") {
        const playerElement = createRetroVideoPlayer(videoSrc);
        placeholder.replaceWith(playerElement);
      }
    });

    scrollToBottom();
  }

  async function sendMessage(formData) {
    if (activeReply) {
      const replyPayload = {
        from: activeReply.from,
        text: activeReply.text || "",
        file: activeReply.file || null,
        timestamp: activeReply.timestamp,
      };
      formData.append("reply_data", JSON.stringify(replyPayload));
    }

    try {
      let url;
      if (CHAT_TYPE === "global") url = "/chat";
      else if (CHAT_TYPE === "dm") url = `/dm/${CHAT_USER}`;
      else if (CHAT_TYPE === "group") url = `/group/${GROUP_ID}`;
      const resp = await fetch(url, {
        method: "POST",
        body: formData,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (resp.ok) {
        const data = await resp.json();
        messageInput.value = "";
        fileInput.value = "";
        messageInput.placeholder = getPlaceholderText();
        messageInput.focus();
        clearReplyPreview();
        if (messageInput.tagName === "TEXTAREA") {
          messageInput.style.height = "auto";
        }
        return data;
      } else {
        alert("Failed to send message.");
      }
    } catch (e) {
      console.error(e);
      alert("Network error.");
    }
    return null;
  }

  function getPlaceholderText() {
    if (CHAT_TYPE === "global") return "Type a message...";
    if (CHAT_TYPE === "dm") return `Message ${CHAT_USER}...`;
    if (CHAT_TYPE === "group")
      return `Message #${groupData?.name || "group"}...`;
    return "Type a message...";
  }

  function initSSE() {
    if (eventSource) eventSource.close();
    let url;
    if (CHAT_TYPE === "global") url = "/stream/global";
    else if (CHAT_TYPE === "dm") url = `/stream/dm/${CHAT_USER}`;
    else if (CHAT_TYPE === "group") url = `/stream/group/${GROUP_ID}`;
    eventSource = new EventSource(url);
    eventSource.onopen = () => {
      reconnectAttempts = 0;
    };
    eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === "heartbeat" || data.type === "connected") return;
        if (data.type === "logout") {
          alert("Another login detected. You will be logged out.");
          window.location.href = "/login";
          return;
        }
        if (data.type === "delete") {
          const msgElement = document.querySelector(
            `.message[data-message-id="${data.message_id}"]`,
          );
          if (msgElement) msgElement.remove();
          return;
        }
        if (data.type === "group_update") {
          if (groupData) groupData.name = data.name;
          const headerTitle = document.querySelector(".chat-header h2");
          if (headerTitle) headerTitle.textContent = `#${data.name}`;
          messageInput.placeholder = `Message #${data.name}...`;
          return;
        }
        if (data.type === "group_deleted") {
          alert("This group has been deleted by the creator.");
          window.location.href = "/groups";
          return;
        }
        if (data.from !== currentUser) addMessage(data, false);
      } catch (err) {
        console.error(err);
      }
    };
    eventSource.onerror = () => {
      // Retry forever with capped exponential backoff; the old code gave
      // up permanently after 5 failed attempts.
      if (eventSource.readyState === EventSource.CLOSED) {
        const delay = Math.min(
          1000 * Math.pow(2, ++reconnectAttempts),
          MAX_RECONNECT_DELAY,
        );
        setTimeout(() => initSSE(), delay);
      }
    };
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const msgText = messageInput.value.trim();
    const file = fileInput.files[0];
    if (!msgText && !file) return;

    const formData = new FormData();
    if (msgText) formData.append("message", msgText);
    if (file) formData.append("file", file);

    sendBtn.disabled = true;
    sendBtn.textContent = "Sending...";
    const result = await sendMessage(formData);
    if (result && result.message) addMessage(result.message, true);
    sendBtn.disabled = false;
    sendBtn.textContent = "Send";
    messageInput.focus();
  }

  function enhanceExistingMessages() {
    if (!messagesContainer) return;
    document.querySelectorAll(".message").forEach((msgDiv) => {
      const ts = msgDiv.getAttribute("data-timestamp");
      const timeSpan = msgDiv.querySelector(".time");
      if (ts && timeSpan) timeSpan.textContent = formatTime(parseInt(ts));

      const from = msgDiv.getAttribute("data-from");
      if (from && from !== currentUser) {
        const senderUser = getUserInfo(from);
        const senderSpan = msgDiv.querySelector(".sender");
        if (senderSpan) {
          const link = senderSpan.querySelector("a");
          if (link) link.textContent = senderUser.display_name;
          else senderSpan.textContent = senderUser.display_name;
        }
        const avatarDiv = msgDiv.querySelector(".avatar");
        if (avatarDiv) avatarDiv.innerHTML = renderAvatar(from);
      }

      // Convert server-rendered <video> tags
      const videoElements = msgDiv.querySelectorAll(".attachment video");
      videoElements.forEach((video) => {
        if (video.closest(".retro-video-player")) return;

        let videoSrc = null;
        const source = video.querySelector("source");
        if (source) videoSrc = source.getAttribute("src");
        if (!videoSrc) videoSrc = video.getAttribute("src");

        if (videoSrc && typeof createRetroVideoPlayer === "function") {
          const container = video.closest(".attachment");
          if (container) {
            const playerElement = createRetroVideoPlayer(videoSrc);
            container.replaceWith(playerElement);
          }
        }
      });

      // Convert placeholder divs (from previously sent messages)
      const videoPlaceholders = msgDiv.querySelectorAll(
        ".attachment[data-video-src]",
      );
      videoPlaceholders.forEach((placeholder) => {
        const videoSrc = placeholder.dataset.videoSrc;
        if (videoSrc && typeof createRetroVideoPlayer === "function") {
          const playerElement = createRetroVideoPlayer(videoSrc);
          placeholder.replaceWith(playerElement);
        }
      });

      const bubble = msgDiv.querySelector(".bubble");
      if (bubble && !bubble.hasAttribute("data-listener")) {
        bubble.setAttribute("data-listener", "true");
        bubble.addEventListener("click", (e) => {
          e.stopPropagation();
          if (
            e.target.tagName === "A" ||
            e.target.tagName === "IMG" ||
            e.target.tagName === "VIDEO" ||
            e.target.closest(".retro-video-player")
          )
            return;
          const messageId = msgDiv.getAttribute("data-message-id");
          const fromAttr = msgDiv.getAttribute("data-from");
          const textElem = msgDiv.querySelector(".text");
          const text = textElem ? textElem.innerText : "";
          showContextMenu(e, {
            id: messageId,
            from: fromAttr,
            text: text,
            file: null,
          });
        });
      }
    });
  }

  function setupEventListeners() {
    if (messageForm) messageForm.addEventListener("submit", handleSubmit);
    if (messageInput) {
      messageInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          messageForm.dispatchEvent(new Event("submit"));
        }
      });
    }
    if (fileInput) {
      fileInput.addEventListener("change", () => {
        if (fileInput.files[0]) {
          messageInput.placeholder = `📎 ${fileInput.files[0].name}`;
        } else {
          messageInput.placeholder = getPlaceholderText();
        }
      });
    }
  }

  // ========== GROUP ADMIN CONTROLS ==========
  function initGroupAdminControls() {
    const editBtn = document.getElementById("editGroupBtn");
    const deleteBtn = document.getElementById("deleteGroupBtn");
    const leaveBtn = document.getElementById("leaveGroupBtn");

    if (editBtn) {
      editBtn.addEventListener("click", async () => {
        const newName = prompt("Enter new group name:", groupData.name);
        if (newName && newName.trim()) {
          const formData = new FormData();
          formData.append("name", newName.trim());
          const resp = await fetch(`/group/${GROUP_ID}/edit`, {
            method: "POST",
            body: formData,
          });
          if (resp.ok) {
            groupData.name = newName.trim();
            document.querySelector(".chat-header h2").textContent =
              `#${newName.trim()}`;
            messageInput.placeholder = `Message #${newName.trim()}...`;
          } else {
            const err = await resp.json();
            alert(err.error || "Failed to edit");
          }
        }
      });
    }

    if (deleteBtn) {
      deleteBtn.addEventListener("click", async () => {
        if (
          confirm(
            "Are you sure you want to delete this group? This action cannot be undone.",
          )
        ) {
          const resp = await fetch(`/group/${GROUP_ID}/delete`, {
            method: "POST",
          });
          if (resp.ok) {
            window.location.href = "/groups";
          } else {
            const err = await resp.json();
            alert(err.error || "Failed to delete");
          }
        }
      });
    }

    if (leaveBtn) {
      leaveBtn.addEventListener("click", async () => {
        if (confirm("Leave this group?")) {
          const resp = await fetch(`/group/${GROUP_ID}/leave`, {
            method: "POST",
          });
          if (resp.ok) {
            window.location.href = "/groups";
          } else {
            const err = await resp.json();
            alert(err.error || "Failed to leave");
          }
        }
      });
    }
  }

  // ========== SIDEBAR & THEME ==========
  function initSidebar() {
    hamburgerBtn = document.getElementById("hamburgerBtn");
    sidebar = document.getElementById("sidebar");
    overlay = document.getElementById("mobileOverlay");
    if (hamburgerBtn && sidebar && overlay) {
      const toggle = () => {
        sidebar.classList.toggle("open");
        overlay.classList.toggle("active");
        document.body.style.overflow = sidebar.classList.contains("open")
          ? "hidden"
          : "";
      };
      hamburgerBtn.addEventListener("click", toggle);
      overlay.addEventListener("click", toggle);
    }
  }

  function applyThemeLabel() {
    if (!themeToggle) return;
    const t = document.documentElement.getAttribute("data-theme");
    themeToggle.textContent =
      t === "dark" ? "☀️ Light Mode" : "🌙 Dark Mode";
  }

  function initTheme() {
    themeToggle = document.getElementById("themeToggle");
    const savedTheme = localStorage.getItem("bobworld-theme") || "light";
    document.documentElement.setAttribute("data-theme", savedTheme);
    applyThemeLabel();
    if (!themeToggle) return;
    themeToggle.addEventListener("click", () => {
      const currentTheme = document.documentElement.getAttribute("data-theme");
      const newTheme = currentTheme === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", newTheme);
      localStorage.setItem("bobworld-theme", newTheme);
      applyThemeLabel();
    });
  }

  function initAvatarPreview() {
    const avatarInput = document.getElementById("avatarInput");
    if (!avatarInput) return;
    avatarInput.addEventListener("change", function (e) {
      const file = e.target.files[0];
      if (file) {
        const reader = new FileReader();
        reader.onload = function (event) {
          let preview = document.getElementById("avatarImg");
          const placeholder = document.getElementById("avatarPlaceholder");
          if (preview) {
            preview.src = event.target.result;
          } else if (placeholder) {
            const img = document.createElement("img");
            img.id = "avatarImg";
            img.src = event.target.result;
            img.className = "profile-avatar-img";
            placeholder.parentNode.replaceChild(img, placeholder);
          } else {
            const container = document.getElementById("avatarPreview");
            if (container) {
              const img = document.createElement("img");
              img.id = "avatarImg";
              img.src = event.target.result;
              img.className = "profile-avatar-img";
              container.innerHTML = "";
              container.appendChild(img);
            }
          }
        };
        reader.readAsDataURL(file);
      }
    });
  }

  function initProfileLinks() {
    document
      .querySelectorAll(
        ".profile-link, .avatar[data-username], .view-profile-btn",
      )
      .forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          let username = el.getAttribute("data-username");
          if (!username && el.classList.contains("avatar"))
            username = el.getAttribute("data-username");
          if (username) window.location.href = `/user/${username}`;
        });
      });
  }

  window.addEventListener("beforeunload", () => {
    if (eventSource) eventSource.close();
  });
})();

/**
 * Retro Video Player - Windows 9x style custom controls
 * Creates a video element with beveled borders and retro controls.
 */
class RetroVideoPlayer {
  constructor(videoSrc, options = {}) {
    this.videoSrc = videoSrc;
    this.options = {
      maxWidth: options.maxWidth || "100%",
      maxHeight: options.maxHeight || "300px",
      autoplay: options.autoplay || false,
      preload: options.preload || "metadata",
    };
    this.container = null;
    this.video = null;
    this.controls = null;
    this.playBtn = null;
    this.progressBar = null;
    this.progressFilled = null;
    this.timeDisplay = null;
    this.muteBtn = null;
    this.fullscreenBtn = null;
    this.isDraggingProgress = false;

    this.build();
  }

  build() {
    // Create container
    this.container = document.createElement("div");
    this.container.className = "retro-video-player";
    this.container.style.maxWidth = this.options.maxWidth;

    // Create video element
    this.video = document.createElement("video");
    this.video.src = this.videoSrc;
    this.video.preload = this.options.preload;
    this.video.style.display = "block";
    this.video.style.width = "100%";
    this.video.style.maxHeight = this.options.maxHeight;
    this.video.style.cursor = "pointer";

    // Create controls bar
    this.controls = document.createElement("div");
    this.controls.className = "retro-video-controls";

    // Play/Pause button
    this.playBtn = document.createElement("button");
    this.playBtn.className = "retro-video-btn";
    this.playBtn.innerHTML = "▶";
    this.playBtn.title = "Play";

    // Progress bar container
    const progressContainer = document.createElement("div");
    progressContainer.className = "retro-progress-container";
    this.progressBar = document.createElement("div");
    this.progressBar.className = "retro-progress-bar";
    this.progressFilled = document.createElement("div");
    this.progressFilled.className = "retro-progress-filled";
    this.progressBar.appendChild(this.progressFilled);
    progressContainer.appendChild(this.progressBar);

    // Time display
    this.timeDisplay = document.createElement("span");
    this.timeDisplay.className = "retro-time-display";
    this.timeDisplay.textContent = "0:00 / 0:00";

    // Mute button
    this.muteBtn = document.createElement("button");
    this.muteBtn.className = "retro-video-btn";
    this.muteBtn.innerHTML = "🔊";
    this.muteBtn.title = "Mute";

    // Fullscreen button
    this.fullscreenBtn = document.createElement("button");
    this.fullscreenBtn.className = "retro-video-btn";
    this.fullscreenBtn.innerHTML = "⛶";
    this.fullscreenBtn.title = "Fullscreen";

    // Assemble controls
    this.controls.appendChild(this.playBtn);
    this.controls.appendChild(progressContainer);
    this.controls.appendChild(this.timeDisplay);
    this.controls.appendChild(this.muteBtn);
    this.controls.appendChild(this.fullscreenBtn);

    // Assemble player
    this.container.appendChild(this.video);
    this.container.appendChild(this.controls);

    // Bind events
    this.bindEvents();
  }

  bindEvents() {
    // Play/Pause
    this.playBtn.addEventListener("click", () => this.togglePlay());
    this.video.addEventListener("click", () => this.togglePlay());

    // Update progress and time
    this.video.addEventListener("timeupdate", () => this.updateProgress());
    this.video.addEventListener("loadedmetadata", () =>
      this.updateTimeDisplay(),
    );
    this.video.addEventListener("durationchange", () =>
      this.updateTimeDisplay(),
    );

    // Progress bar seeking
    this.progressBar.addEventListener("mousedown", (e) => {
      this.isDraggingProgress = true;
      this.seekToPosition(e);
    });
    document.addEventListener("mousemove", (e) => {
      if (this.isDraggingProgress) {
        this.seekToPosition(e);
      }
    });
    document.addEventListener("mouseup", () => {
      this.isDraggingProgress = false;
    });

    // Mute toggle
    this.muteBtn.addEventListener("click", () => this.toggleMute());

    // Fullscreen toggle
    this.fullscreenBtn.addEventListener("click", () => this.toggleFullscreen());
    document.addEventListener("fullscreenchange", () =>
      this.updateFullscreenIcon(),
    );
    document.addEventListener("webkitfullscreenchange", () =>
      this.updateFullscreenIcon(),
    );
    document.addEventListener("mozfullscreenchange", () =>
      this.updateFullscreenIcon(),
    );
    document.addEventListener("MSFullscreenChange", () =>
      this.updateFullscreenIcon(),
    );

    // Update play button icon on play/pause
    this.video.addEventListener("play", () => {
      this.playBtn.innerHTML = "⏸";
      this.playBtn.title = "Pause";
    });
    this.video.addEventListener("pause", () => {
      this.playBtn.innerHTML = "▶";
      this.playBtn.title = "Play";
    });
    this.video.addEventListener("ended", () => {
      this.playBtn.innerHTML = "▶";
      this.playBtn.title = "Play";
    });

    // Volume change
    this.video.addEventListener("volumechange", () => this.updateMuteIcon());
  }

  togglePlay() {
    if (this.video.paused) {
      this.video.play().catch((e) => console.log("Play prevented:", e));
    } else {
      this.video.pause();
    }
  }

  toggleMute() {
    this.video.muted = !this.video.muted;
    this.updateMuteIcon();
  }

  updateMuteIcon() {
    if (this.video.muted || this.video.volume === 0) {
      this.muteBtn.innerHTML = "🔇";
      this.muteBtn.title = "Unmute";
    } else {
      this.muteBtn.innerHTML = "🔊";
      this.muteBtn.title = "Mute";
    }
  }

  toggleFullscreen() {
    if (!document.fullscreenElement) {
      // Enter fullscreen
      if (this.container.requestFullscreen) {
        this.container.requestFullscreen();
      } else if (this.container.webkitRequestFullscreen) {
        this.container.webkitRequestFullscreen();
      } else if (this.container.mozRequestFullScreen) {
        this.container.mozRequestFullScreen();
      } else if (this.container.msRequestFullscreen) {
        this.container.msRequestFullscreen();
      }
    } else {
      // Exit fullscreen
      if (document.exitFullscreen) {
        document.exitFullscreen();
      } else if (document.webkitExitFullscreen) {
        document.webkitExitFullscreen();
      } else if (document.mozCancelFullScreen) {
        document.mozCancelFullScreen();
      } else if (document.msExitFullscreen) {
        document.msExitFullscreen();
      }
    }
  }

  updateFullscreenIcon() {
    if (document.fullscreenElement) {
      this.fullscreenBtn.innerHTML = "✕";
      this.fullscreenBtn.title = "Exit Fullscreen";
    } else {
      this.fullscreenBtn.innerHTML = "⛶";
      this.fullscreenBtn.title = "Fullscreen";
    }
  }

  seekToPosition(e) {
    const rect = this.progressBar.getBoundingClientRect();
    const pos = (e.clientX - rect.left) / rect.width;
    const seekTime = pos * this.video.duration;
    if (isFinite(seekTime)) {
      this.video.currentTime = seekTime;
    }
  }

  updateProgress() {
    if (this.video.duration) {
      const percent = (this.video.currentTime / this.video.duration) * 100;
      this.progressFilled.style.width = percent + "%";
    }
    this.updateTimeDisplay();
  }

  updateTimeDisplay() {
    const current = this.formatTime(this.video.currentTime);
    const duration = this.formatTime(this.video.duration);
    this.timeDisplay.textContent = `${current} / ${duration}`;
  }

  formatTime(seconds) {
    if (isNaN(seconds)) return "0:00";
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  getElement() {
    return this.container;
  }
}

// Helper to detect if a file is a video
function isVideoFile(filename) {
  return /\.(mp4|webm|ogg|mov|avi|mkv|wmv|flv|m4v)$/i.test(filename);
}

// Function to create a retro video player from a file path
function createRetroVideoPlayer(videoPath) {
  const player = new RetroVideoPlayer(videoPath, {
    maxWidth: "100%",
    maxHeight: "300px",
  });
  return player.getElement();
}

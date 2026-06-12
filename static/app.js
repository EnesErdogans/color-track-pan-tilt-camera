const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const presets = {
  red: { lower_h: 170, upper_h: 10, lower_s: 120, upper_s: 255, lower_v: 70, upper_v: 255 },
  green: { lower_h: 35, upper_h: 85, lower_s: 70, upper_s: 255, lower_v: 50, upper_v: 255 },
  blue: { lower_h: 90, upper_h: 135, lower_s: 90, upper_s: 255, lower_v: 50, upper_v: 255 },
  yellow: { lower_h: 20, upper_h: 35, lower_s: 100, upper_s: 255, lower_v: 100, upper_v: 255 },
};

const settingIds = [
  "lower_h", "lower_s", "lower_v", "upper_h", "upper_s", "upper_v",
  "min_area", "dead_zone_x", "dead_zone_y", "gain", "max_step",
];

let settingsLoaded = false;
let calibrationLoaded = false;
let serialConnected = false;
let calibrationPan = 90;
let calibrationTilt = 90;
let savedAimCenter = { x: 0.5, y: 0.5 };
let pendingAimCenter = null;
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = error ? "show error" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = ""; }, 2800);
}

function setBadge(element, text, state = "") {
  element.textContent = text;
  element.className = `badge ${state}`;
}

function videoContentBox() {
  const video = $("#video");
  const shell = video.parentElement;
  const shellRect = shell.getBoundingClientRect();
  const sourceWidth = video.naturalWidth || 640;
  const sourceHeight = video.naturalHeight || 480;
  const scale = Math.min(shellRect.width / sourceWidth, shellRect.height / sourceHeight);
  const width = sourceWidth * scale;
  const height = sourceHeight * scale;
  return {
    left: (shellRect.width - width) / 2,
    top: (shellRect.height - height) / 2,
    width,
    height,
  };
}

function renderAimMarker(center) {
  const box = videoContentBox();
  const marker = $("#aimMarker");
  marker.style.left = `${box.left + center.x * box.width}px`;
  marker.style.top = `${box.top + center.y * box.height}px`;
}

function calibrationPayload() {
  return {
    pan: calibrationPan,
    tilt: calibrationTilt,
  };
}

function renderCalibrationValues() {
  $("#panCenterValue").textContent = calibrationPan;
  $("#tiltCenterValue").textContent = calibrationTilt;
}

function renderNetworkUrls(urls) {
  const container = $("#networkUrls");
  if (!urls.length) {
    container.textContent = "Yerel ag adresi bulunamadi.";
    return;
  }
  container.innerHTML = "";
  urls.forEach((url) => {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = url;
    link.target = "_blank";
    link.rel = "noreferrer";
    container.appendChild(link);
  });
}

async function scanCameras() {
  const button = $("#scanCameras");
  button.disabled = true;
  try {
    const data = await api("/api/cameras");
    const select = $("#cameraSelect");
    select.innerHTML = "";
    if (!data.cameras.length) {
      select.innerHTML = '<option value="">Kamera bulunamadi</option>';
    }
    data.cameras.forEach((camera) => {
      const option = document.createElement("option");
      option.value = camera.index;
      option.textContent = `${camera.name}${camera.active ? " (aktif)" : ""}`;
      select.appendChild(option);
    });
    const active = data.cameras.find((camera) => camera.active);
    if (active) {
      select.value = String(active.index);
    } else if (data.cameras.length) {
      await selectCamera(data.cameras[0].index, true);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function selectCamera(index, automatic = false) {
  await api("/api/camera", {
    method: "POST",
    body: JSON.stringify({ index: Number(index) }),
  });
  if (!automatic) toast("Kamera acildi.");
  await refreshStatus();
}

async function scanPorts() {
  try {
    const data = await api("/api/serial/ports");
    const select = $("#serialSelect");
    select.innerHTML = "";
    if (!data.ports.length) {
      select.innerHTML = '<option value="">Seri port bulunamadi</option>';
    }
    data.ports.forEach((port) => {
      const option = document.createElement("option");
      option.value = port.device;
      option.textContent = `${port.device} - ${port.description}`;
      select.appendChild(option);
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function setMode(mode) {
  try {
    await api("/api/mode", { method: "POST", body: JSON.stringify({ mode }) });
    renderMode(mode);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderMode(mode) {
  const autonomous = mode === "autonomous";
  $("#manualMode").classList.toggle("active", !autonomous);
  $("#autoMode").classList.toggle("active", autonomous);
  $("#trackingPanel").classList.toggle("hidden", !autonomous);
  $("#manualPanel").classList.toggle("hidden", autonomous);
  setBadge($("#modeBadge"), autonomous ? "OTONOM" : "MANUEL", "accent");
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    $("#cameraTitle").textContent = status.camera_name;
    $("#fps").textContent = status.fps;
    $("#targetState").textContent = status.target_found ? "VAR" : "YOK";
    setBadge(
      $("#cameraBadge"),
      status.camera_index === null ? "Kamera bekleniyor" : `${status.camera_name} aktif`,
      status.camera_index === null ? "" : "ok",
    );
    setBadge(
      $("#serialBadge"),
      status.serial_connected ? `${status.serial_port} bagli` : "Arduino bagli degil",
      status.serial_connected ? "ok" : (status.serial_error ? "error" : ""),
    );
    serialConnected = status.serial_connected;
    $$(
      ".dpad button, #centerButton, #laserToggle, " +
      ".calibration-dpad button"
    ).forEach((element) => {
      element.disabled = !serialConnected;
    });
    renderNetworkUrls(status.network_urls || []);
    $("#savedCalibration").textContent =
      `Kayitli merkez: pan ${status.servo_calibration.pan}, ` +
      `tilt ${status.servo_calibration.tilt}`;
    if (status.serial_connected && !calibrationLoaded) {
      calibrationPan = status.servo_calibration.pan;
      calibrationTilt = status.servo_calibration.tilt;
      renderCalibrationValues();
      calibrationLoaded = true;
    }
    renderMode(status.mode);
    savedAimCenter = status.aim_center;
    if (pendingAimCenter === null) {
      renderAimMarker(savedAimCenter);
    }
    if (!settingsLoaded) {
      settingIds.forEach((id) => { $(`#${id}`).value = status.settings[id]; });
      settingsLoaded = true;
    }
  } catch {
    setBadge($("#cameraBadge"), "Sunucuya ulasilamiyor", "error");
  }
}

$("#scanCameras").addEventListener("click", scanCameras);
$("#scanPorts").addEventListener("click", scanPorts);

$("#useCamera").addEventListener("click", async () => {
  const index = $("#cameraSelect").value;
  if (index === "") return toast("Once kamera secin.", true);
  try {
    await selectCamera(index);
  } catch (error) {
    toast(error.message, true);
  }
});

$("#connectSerial").addEventListener("click", async () => {
  const port = $("#serialSelect").value;
  if (!port) return toast("Once seri port secin.", true);
  try {
    await api("/api/serial/connect", { method: "POST", body: JSON.stringify({ port }) });
    calibrationLoaded = false;
    toast("Arduino baglandi.");
    await refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
});

$("#disconnectSerial").addEventListener("click", async () => {
  await api("/api/serial/disconnect", { method: "POST", body: "{}" });
  toast("Arduino baglantisi kesildi.");
  refreshStatus();
});

$("#manualMode").addEventListener("click", () => setMode("manual"));
$("#autoMode").addEventListener("click", () => setMode("autonomous"));

$("#moveStep").addEventListener("input", (event) => {
  $("#moveStepValue").textContent = event.target.value;
});

$("#calibrationStep").addEventListener("input", (event) => {
  $("#calibrationStepValue").textContent = event.target.value;
});

$$(".calibration-dpad button[data-cal-pan]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!serialConnected) return toast("Once Arduino seri portuna baglanin.", true);
    const step = Number($("#calibrationStep").value);
    const nextPan = Math.max(
      10,
      Math.min(170, calibrationPan + Number(button.dataset.calPan) * step),
    );
    const nextTilt = Math.max(
      20,
      Math.min(160, calibrationTilt + Number(button.dataset.calTilt) * step),
    );
    try {
      await api("/api/calibration/preview", {
        method: "POST",
        body: JSON.stringify({ pan: nextPan, tilt: nextTilt }),
      });
      calibrationPan = nextPan;
      calibrationTilt = nextTilt;
      renderCalibrationValues();
    } catch (error) {
      toast(error.message, true);
    }
  });
});

$("#saveCalibration").addEventListener("click", async () => {
  if (!serialConnected) return toast("Once Arduino seri portuna baglanin.", true);
  try {
    const data = await api("/api/calibration/save", {
      method: "POST",
      body: JSON.stringify(calibrationPayload()),
    });
    $("#savedCalibration").textContent =
      `Kayitli merkez: pan ${data.calibration.pan}, tilt ${data.calibration.tilt}`;
    toast("Servo merkezleri Arduino EEPROM'una kaydedildi.");
  } catch (error) {
    toast(error.message, true);
  }
});

$$(".dpad button[data-pan]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!serialConnected) return toast("Once Arduino seri portuna baglanin.", true);
    const step = Number($("#moveStep").value);
    const pan = Number(button.dataset.pan) * step;
    const tilt = Number(button.dataset.tilt) * step;
    try {
      await api("/api/move", { method: "POST", body: JSON.stringify({ pan, tilt }) });
    } catch (error) {
      toast(error.message, true);
    }
  });
});

$("#centerButton").addEventListener("click", async () => {
  try {
    await api("/api/center", { method: "POST", body: "{}" });
    toast("Kamera merkeze alindi.");
  } catch (error) {
    toast(error.message, true);
  }
});

$("#laserToggle").addEventListener("change", async (event) => {
  try {
    await api("/api/laser", {
      method: "POST",
      body: JSON.stringify({ enabled: event.target.checked }),
    });
  } catch (error) {
    toast(error.message, true);
  }
});

$("#video").addEventListener("click", (event) => {
  const shellRect = event.currentTarget.parentElement.getBoundingClientRect();
  const box = videoContentBox();
  const x = (event.clientX - shellRect.left - box.left) / box.width;
  const y = (event.clientY - shellRect.top - box.top) / box.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  pendingAimCenter = { x, y };
  renderAimMarker(pendingAimCenter);
  toast("Nisangah noktasi secildi. Kaydet tusuna basin.");
});

$("#saveAimCenter").addEventListener("click", async () => {
  if (pendingAimCenter === null) {
    return toast("Once canli goruntu uzerinde bir nokta secin.", true);
  }
  try {
    const data = await api("/api/aim-center", {
      method: "POST",
      body: JSON.stringify(pendingAimCenter),
    });
    savedAimCenter = data.aim_center;
    pendingAimCenter = null;
    renderAimMarker(savedAimCenter);
    toast("Nisangah merkezi kalici olarak kaydedildi.");
  } catch (error) {
    toast(error.message, true);
  }
});

$("#resetAimCenter").addEventListener("click", async () => {
  try {
    const data = await api("/api/aim-center", {
      method: "POST",
      body: JSON.stringify({ reset: true }),
    });
    savedAimCenter = data.aim_center;
    pendingAimCenter = null;
    renderAimMarker(savedAimCenter);
    toast("Nisangah goruntunun ortasina alindi.");
  } catch (error) {
    toast(error.message, true);
  }
});

window.addEventListener("resize", () => {
  renderAimMarker(pendingAimCenter || savedAimCenter);
});

$$(".preset").forEach((button) => {
  button.addEventListener("click", () => {
    const values = presets[button.dataset.preset];
    Object.entries(values).forEach(([key, value]) => { $(`#${key}`).value = value; });
  });
});

$("#saveTracking").addEventListener("click", async () => {
  const payload = {};
  settingIds.forEach((id) => { payload[id] = Number($(`#${id}`).value); });
  try {
    const data = await api("/api/tracking", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    settingIds.forEach((id) => { $(`#${id}`).value = data.settings[id]; });
    toast("Takip ayarlari uygulandi.");
  } catch (error) {
    toast(error.message, true);
  }
});

scanPorts();
scanCameras();
refreshStatus();
setInterval(refreshStatus, 1000);

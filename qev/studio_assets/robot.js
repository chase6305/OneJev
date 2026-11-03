"use strict";
window.OneJevRobot = (() => {
  const skills = {
    approach: "靠近方块",
    grasp: "闭合夹爪",
    lift: "抬起方块",
    place: "放入托盘",
    recover: "重新抓取",
    retreat: "收回机械臂",
  };
  let state = null,
    working = false,
    initialized = false,
    starting = null,
    selected = null,
    auto = false,
    timer = null;
  const active = () => !$("panel-robot").hidden;
  const isReady = () =>
    document.body.dataset.modelState === "ready" && state?.clients > 0;
  const displayPercent = (id, value) => {
    $(id).innerHTML = `${pct(value)}<small>%</small>`;
  };
  function agreement(record) {
    if (!record) return "等待观察";
    let uncertain = false;
    for (const key of ["placed", "grasped"]) {
      const p = record.answers?.[key]?.noul;
      if (!Number.isFinite(p) || (p > 0.15 && p < 0.85)) uncertain = true;
      else if (p >= 0.85 !== record.truth[key]) return "需要复核";
    }
    return uncertain ? "模型不确定" : "判断与场景一致";
  }
  function stopAuto() {
    auto = false;
    clearTimeout(timer);
    timer = null;
  }
  function render() {
    if (!state) return;
    const busy = working || state.busy;
    const latest = state.history.at(-1);
    const record = selected === null ? latest : state.history[selected];
    const fresh = Boolean(
      record &&
        (selected !== null || record.revision === state.revision) &&
        !busy,
    );
    const truth =
      selected === null ? state.truth : record?.truth || state.truth;
    $("robot-phase").textContent = busy ? "正在执行技能…" : state.phase_label;
    $("robot-connection").textContent = state.clients
      ? "三维视图已连接"
      : "等待三维视图连接…";
    $("robot-step").textContent = state.next_skill
      ? `执行：${skills[state.next_skill]}`
      : "动作已完成";
    $("robot-next").textContent =
      skills[state.next_skill] || "动作已完成，可重置场景";
    const requireModel = $("robot-auto-judge").checked;
    $("robot-step").disabled =
      busy ||
      auto ||
      !state.next_skill ||
      !state.clients ||
      (requireModel && !isReady());
    $("robot-observe").disabled = busy || auto || !isReady();
    $("robot-reset").disabled = busy;
    $("robot-auto").textContent = auto ? "Ⅱ 暂停演示" : "▶ 自动演示";
    $("robot-auto").disabled =
      !auto &&
      (busy ||
        !state.next_skill ||
        !state.clients ||
        (requireModel && !isReady()));
    $("robot-miss").disabled =
      busy || !["ready", "hover"].includes(state.phase);
    $("robot-drop").disabled = busy || !state.truth.grasped;
    $("robot-shift").disabled = busy;
    $("robot-auto-judge").disabled = busy || auto;
    $("robot-fault-hint").textContent = state.miss_next
      ? "已设置：下一次抓取将抓空。"
      : "故障改变场景状态，不会修改模型返回的概率。";
    displayPercent(
      "robot-placed",
      fresh ? record.answers.placed?.noul : undefined,
    );
    displayPercent(
      "robot-grasped",
      fresh ? record.answers.grasped?.noul : undefined,
    );
    for (const key of ["placed", "grasped"]) {
      const p = fresh ? record.answers[key]?.noul : 0;
      $(`robot-${key}-fill`).style.width = Number.isFinite(p)
        ? `${Math.max(0, Math.min(100, p * 100))}%`
        : "0";
      $(`robot-truth-${key}`).textContent = truth[key] ? "是" : "否";
    }
    $("robot-verdict").textContent = busy
      ? "等待动作完成"
      : fresh
        ? agreement(record)
        : "场景已变化，待判断";
    $("robot-verdict").className =
      `badge ${fresh && agreement(record) === "判断与场景一致" ? "good" : fresh ? "bad" : ""}`;
    $("robot-time").textContent = fresh ? ms(record.request_s) : "—";
    $("robot-error").hidden = !state.error;
    $("robot-error").textContent = state.error || "";
    $("robot-raw").textContent = record
      ? JSON.stringify(record, null, 2)
      : "暂无结果";
    if (record) {
      if ($("robot-camera").getAttribute("src") !== record.image)
        $("robot-camera").src = record.image;
      $("robot-camera").hidden = false;
      $("robot-camera-empty").hidden = true;
    } else {
      $("robot-camera").hidden = true;
      $("robot-camera-empty").hidden = false;
    }
    $("robot-camera-label").textContent =
      selected === null
        ? "固定相机 · 与自由视角独立"
        : `回看第 ${selected + 1} 次检查 · 三维视图保持当前状态`;
    $("robot-latest").hidden = selected === null;
    const signature = `${state.id}:${state.history.length}:${selected}`;
    if ($("robot-history").dataset.signature !== signature) {
      $("robot-history").dataset.signature = signature;
      $("robot-history").innerHTML = state.history.length
        ? state.history
            .map(
              (item, index) =>
                `<button class="step-card ${index === (selected ?? state.history.length - 1) ? "selected" : ""}" data-robot-history="${index}"><img src="${esc(item.image)}" alt="第 ${index + 1} 次机器人视觉检查"><div><strong>${index + 1} · ${esc(item.phase)}</strong><small>放置完成 ${pct(item.answers.placed?.noul)}%</small></div></button>`,
            )
            .join("")
        : '<div class="empty-line">还没有视觉检查记录。</div>';
      $("robot-history")
        .querySelectorAll("[data-robot-history]")
        .forEach((button) =>
          button.addEventListener("click", () => {
            selected = Number(button.dataset.robotHistory);
            render();
          }),
        );
    }
  }
  async function perform(action, body = {}) {
    if (working) return false;
    working = true;
    selected = null;
    render();
    try {
      state = await api(`/api/robot/${action}`, body);
      if (state.error) {
        stopAuto();
        toast(state.error);
      }
      if (
        auto &&
        $("robot-auto-judge").checked &&
        agreement(state.history.at(-1)) !== "判断与场景一致"
      ) {
        stopAuto();
        toast("模型判断需要复核，自动演示已暂停。你仍可手动检查与执行技能。");
      }
      return true;
    } catch (error) {
      stopAuto();
      toast(error.message);
      return false;
    } finally {
      working = false;
      render();
    }
  }
  async function playNext() {
    if (!auto || !state?.next_skill) {
      stopAuto();
      render();
      return;
    }
    await perform("step", { judge: $("robot-auto-judge").checked });
    if (auto && state.next_skill) timer = setTimeout(playNext, 3000);
    else {
      stopAuto();
      render();
    }
  }
  async function refresh() {
    if (active() && !working && initialized) {
      try {
        state = await api("/api/robot");
        render();
      } catch (error) {
        $("robot-connection").textContent = "三维连接中断，请重试";
      }
    }
    setTimeout(refresh, 1000);
  }
  async function activate() {
    if (starting) return starting;
    starting = (async () => {
      try {
        state = await api("/api/robot");
        if (!initialized) {
          const protocol = location.protocol === "https:" ? "wss:" : "ws:";
          $("robot-viewer").src =
            `/robot-view/?websocket=${encodeURIComponent(`${protocol}//${location.host}/robot-ws`)}`;
          initialized = true;
        }
        render();
      } catch (error) {
        toast(error.message);
        $("robot-connection").textContent = error.message;
      } finally {
        starting = null;
      }
    })();
    return starting;
  }
  $("robot-step").addEventListener("click", () =>
    perform("step", { judge: $("robot-auto-judge").checked }),
  );
  $("robot-observe").addEventListener("click", () => perform("observe"));
  $("robot-reset").addEventListener("click", () => {
    stopAuto();
    perform("reset");
  });
  $("robot-auto").addEventListener("click", () => {
    if (auto) {
      stopAuto();
      render();
    } else {
      auto = true;
      playNext();
    }
  });
  $("robot-auto-judge").addEventListener("change", render);
  for (const kind of ["miss", "drop", "shift"])
    $("robot-" + kind).addEventListener("click", () => {
      stopAuto();
      perform("fault", { kind });
    });
  $("robot-latest").addEventListener("click", () => {
    selected = null;
    render();
  });
  refresh();
  if (location.hash === "#robot") activate();
  return { activate };
})();

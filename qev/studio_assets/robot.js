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
    archive = null,
    working = false,
    initialized = false,
    starting = null;
  let viewRevision = 0,
    runsLoaded = false,
    loadingArchive = null;
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
  function accept(next) {
    const changed = state && state.id !== next.id;
    state = next;
    if (changed) {
      archive = null;
      loadRuns();
    }
    if (
      state.replay &&
      state.replay.run_id !== state.id &&
      archive?.id !== state.replay.run_id
    )
      loadArchive(state.replay.run_id);
    render();
  }
  async function loadArchive(identifier) {
    if (loadingArchive === identifier) return;
    loadingArchive = identifier;
    try {
      const result = await api(`/api/robot/runs/${identifier}`);
      if (state?.replay?.run_id === identifier) {
        archive = result;
        render();
      }
    } catch (error) {
      toast(error.message);
    } finally {
      if (loadingArchive === identifier) loadingArchive = null;
    }
  }
  function render() {
    if (!state) return;
    const busy = working || state.busy;
    const auto = state.auto_running;
    const replay = state.replay;
    const source =
      replay && replay.run_id !== state.id
        ? archive?.id === replay.run_id
          ? archive
          : null
        : state;
    const latest = state.history.at(-1);
    const record = replay ? replay.record : latest;
    const fresh = Boolean(
      record && (replay || record.revision === state.revision) && !busy,
    );
    const truth = replay?.truth || state.truth;
    $("robot-phase").textContent = replay
      ? "历史姿态 · 不执行动作"
      : busy
        ? "正在执行技能…"
        : state.phase_label;
    $("robot-connection").textContent = state.clients
      ? "三维视图已连接"
      : "等待三维视图连接…";
    $("robot-step").textContent = state.next_skill
      ? `执行：${skills[state.next_skill]}`
      : "动作已完成";
    $("robot-next").textContent = replay
      ? "返回当前场景后继续"
      : skills[state.next_skill] || "动作已完成，可重置场景";
    const requireModel = $("robot-auto-judge").checked;
    $("robot-step").disabled =
      busy ||
      auto ||
      !!replay ||
      !state.next_skill ||
      !state.clients ||
      (requireModel && !isReady());
    $("robot-observe").disabled = busy || auto || !!replay || !isReady();
    $("robot-reset").disabled = busy || auto || !!replay;
    $("robot-auto").textContent = auto ? "Ⅱ 暂停演示" : "▶ 自动演示";
    $("robot-auto").disabled =
      working ||
      (!auto &&
        (busy ||
          !!replay ||
          !state.next_skill ||
          !state.clients ||
          (requireModel && !isReady())));
    $("robot-miss").disabled =
      busy || auto || !!replay || !["ready", "hover"].includes(state.phase);
    $("robot-drop").disabled = busy || auto || !!replay || !state.truth.grasped;
    $("robot-shift").disabled = busy || auto || !!replay;
    $("robot-auto-judge").disabled = busy || auto || !!replay;
    $("robot-auto-judge").checked = state.auto_judge;
    for (const id of ["robot-delay", "robot-speed"])
      $(id).disabled = working || !!replay;
    if (document.activeElement !== $("robot-delay"))
      $("robot-delay").value = state.delay;
    if (document.activeElement !== $("robot-speed"))
      $("robot-speed").value = state.motion_speed;
    $("robot-delay-value").textContent = `${$("robot-delay").value} 秒`;
    $("robot-playback-status").textContent = replay
      ? "回放不会调用模型或执行技能"
      : auto
        ? state.busy
          ? "自动演示中 · 可随时暂停"
          : state.next_step_at
            ? `${Math.max(0, Math.ceil(state.next_step_at - Date.now() / 1000))} 秒后执行下一动作`
            : "正在开始演示…"
        : state.pause_reason || "逐步执行，随时观察";
    $("robot-replay-notice").hidden = !replay;
    $("robot-replay-label").textContent = replay
      ? `${replay.label} · ${replay.run_id === state.id ? "本次演示" : "历史演示"} · 所有标签页同步，仅显示记录。`
      : "";
    $("robot-live").disabled = busy;
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
        : replay
          ? "该姿态没有视觉判断"
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
    $("robot-camera-label").textContent = replay
      ? `${replay.label} · 三维视图与记录同步`
      : "固定相机 · 与自由视角独立";
    $("robot-latest").hidden = !replay;
    $("robot-latest").disabled = busy;
    $("robot-report").href = `/api/robot/runs/${replay?.run_id || state.id}`;
    $("robot-run").value =
      replay && replay.run_id !== state.id ? replay.run_id : "";
    $("robot-run").disabled = busy || auto;
    const events = source?.events || [];
    const eventSignature = `${source?.id}:${events.length}:${replay?.event_index}:${busy}:${auto}`;
    if ($("robot-events").dataset.signature !== eventSignature) {
      $("robot-events").dataset.signature = eventSignature;
      $("robot-events").innerHTML =
        events
          .map(
            (item, index) =>
              `<button class="step-card robot-event ${replay?.event_index === index ? "selected" : ""}" data-robot-event="${index}" ${busy || auto ? "disabled" : ""}><span class="robot-event-index">${String(index + 1).padStart(2, "0")}</span><div><strong>${esc(item.label)}</strong><small>${item.kind === "fault" ? "故障注入" : item.kind === "reset" ? "初始姿态" : `${item.duration_s.toFixed(1)} 秒`} · 点击回放</small></div></button>`,
          )
          .join("") || '<div class="empty-line">该记录没有动作轨迹。</div>';
      $("robot-events")
        .querySelectorAll("[data-robot-event]")
        .forEach((button) =>
          button.addEventListener("click", () =>
            perform("replay", {
              source_id: source.id,
              event_index: Number(button.dataset.robotEvent),
            }),
          ),
        );
    }
    const history = source?.history || [];
    const signature = `${source?.id}:${history.length}:${replay?.history_index}:${busy}:${auto}`;
    if ($("robot-history").dataset.signature !== signature) {
      $("robot-history").dataset.signature = signature;
      $("robot-history").innerHTML = history.length
        ? history
            .map(
              (item, index) =>
                `<button class="step-card ${replay?.history_index === index ? "selected" : ""}" data-robot-history="${index}" ${busy || auto || !item.pose ? "disabled" : ""}><img src="${esc(item.image)}" alt="第 ${index + 1} 次机器人视觉检查"><div><strong>${index + 1} · ${esc(item.phase)}</strong><small>放置完成 ${pct(item.answers.placed?.noul)}%${item.pose ? "" : " · 旧版截图"}</small></div></button>`,
            )
            .join("")
        : '<div class="empty-line">还没有视觉检查记录。</div>';
      $("robot-history")
        .querySelectorAll("[data-robot-history]")
        .forEach((button) =>
          button.addEventListener("click", () =>
            perform("replay", {
              source_id: source.id,
              history_index: Number(button.dataset.robotHistory),
            }),
          ),
        );
    }
  }
  async function perform(action, body = {}) {
    if (working || !state) return false;
    const epoch = ++viewRevision;
    working = true;
    const control = { run_id: state.id, expected_revision: state.revision };
    render();
    try {
      const result = await api(`/api/robot/${action}`, { ...control, ...body });
      if (epoch === viewRevision) accept(result);
      if (result.error) toast(result.error);
      return true;
    } catch (error) {
      toast(error.message);
      try {
        const next = await api("/api/robot");
        if (epoch === viewRevision) accept(next);
      } catch (_) {}
      return false;
    } finally {
      working = false;
      render();
    }
  }
  async function loadRuns() {
    try {
      const result = await api("/api/robot/runs");
      if (!state) return;
      $("robot-run").innerHTML =
        '<option value="">当前演示</option>' +
        result.runs
          .filter((run) => run.id !== state.id)
          .map(
            (run) =>
              `<option value="${esc(run.id)}">${esc(run.created_at ? new Date(run.created_at).toLocaleString() : run.id.slice(0, 8))} · ${esc(run.phase_label)} · ${run.events} 帧</option>`,
          )
          .join("");
      runsLoaded = true;
      render();
    } catch (error) {
      toast(error.message);
    }
  }
  async function refresh() {
    if (active() && !working && initialized) {
      const epoch = viewRevision;
      try {
        const next = await api("/api/robot");
        if (epoch === viewRevision && !working) accept(next);
      } catch (_) {
        $("robot-connection").textContent = "三维连接中断，请重试";
      }
    }
    setTimeout(refresh, 700);
  }
  async function activate() {
    if (starting) return starting;
    starting = (async () => {
      const epoch = viewRevision;
      try {
        const next = await api("/api/robot");
        if (epoch !== viewRevision) return;
        accept(next);
        if (!initialized) {
          const protocol = location.protocol === "https:" ? "wss:" : "ws:";
          $("robot-viewer").src =
            `/robot-view/?websocket=${encodeURIComponent(`${protocol}//${location.host}/robot-ws`)}`;
          initialized = true;
        }
        if (!runsLoaded) await loadRuns();
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
  const playbackOptions = () => ({
    judge: $("robot-auto-judge").checked,
    delay: Number($("robot-delay").value),
    motion_speed: Number($("robot-speed").value),
  });
  $("robot-step").addEventListener("click", () =>
    perform("step", { judge: $("robot-auto-judge").checked }),
  );
  $("robot-observe").addEventListener("click", () => perform("observe"));
  $("robot-reset").addEventListener("click", () => perform("reset"));
  $("robot-auto").addEventListener("click", () =>
    perform(state?.auto_running ? "pause" : "auto", playbackOptions()),
  );
  $("robot-auto-judge").addEventListener("change", () =>
    perform("settings", playbackOptions()),
  );
  $("robot-delay").addEventListener("input", () => {
    $("robot-delay-value").textContent = `${$("robot-delay").value} 秒`;
  });
  for (const id of ["robot-delay", "robot-speed"])
    $(id).addEventListener("change", () =>
      perform("settings", playbackOptions()),
    );
  for (const kind of ["miss", "drop", "shift"])
    $("robot-" + kind).addEventListener("click", () =>
      perform("fault", { kind }),
    );
  for (const id of ["robot-latest", "robot-live"])
    $(id).addEventListener("click", () => perform("live"));
  $("robot-runs-refresh").addEventListener("click", loadRuns);
  $("robot-run").addEventListener("change", async () => {
    const identifier = $("robot-run").value;
    if (!identifier) {
      perform("live");
      return;
    }
    const runId = state?.id,
      revision = state?.revision;
    try {
      const report = await api(`/api/robot/runs/${identifier}`);
      if (state?.id !== runId || state?.revision !== revision || working) {
        render();
        return;
      }
      const ok = await perform(
        "replay",
        report.events?.length
          ? { source_id: identifier, event_index: report.events.length - 1 }
          : { source_id: identifier, history_index: report.history.length - 1 },
      );
      if (ok && state.replay?.run_id === identifier) {
        archive = report;
        render();
      }
    } catch (error) {
      toast(error.message);
      render();
    }
  });
  refresh();
  if (location.hash === "#robot") activate();
  return { activate };
})();

const state = {
  mode: "system",
  direction: "EN-ZH",
  practice: null,
  review: null,
  sessionKey: null,
  currentIndex: 0,
};

const $ = (id) => document.getElementById(id);

const recorderState = { recorder: null, stream: null, chunks: [], startedAt: 0, timer: null, shouldTranscribe: false, audioContext: null, analyser: null, levelTimer: null, silenceStartedAt: 0, pauses: [] };

function formatVoiceTime(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function setVoiceStatus(text) {
  $("voice-status").textContent = text;
}

function getRecordingMimeType() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return options.find((type) => MediaRecorder.isTypeSupported?.(type)) || "";
}

function beginPauseSampling(stream) {
  try {
    recorderState.audioContext = new AudioContext();
    const source = recorderState.audioContext.createMediaStreamSource(stream);
    recorderState.analyser = recorderState.audioContext.createAnalyser();
    recorderState.analyser.fftSize = 512;
    source.connect(recorderState.analyser);
    const data = new Uint8Array(recorderState.analyser.fftSize);
    recorderState.pauses = [];
    recorderState.levelTimer = setInterval(() => {
      recorderState.analyser.getByteTimeDomainData(data);
      const rms = Math.sqrt(data.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / data.length);
      const now = Date.now();
      if (rms < 0.035) {
        if (!recorderState.silenceStartedAt) recorderState.silenceStartedAt = now;
      } else if (recorderState.silenceStartedAt) {
        const duration = now - recorderState.silenceStartedAt;
        if (duration >= 500) recorderState.pauses.push({ start_ms: recorderState.silenceStartedAt - recorderState.startedAt, duration_ms: duration });
        recorderState.silenceStartedAt = 0;
      }
    }, 100);
  } catch {
    recorderState.audioContext = null;
  }
}

function endPauseSampling() {
  if (recorderState.silenceStartedAt) {
    const duration = Date.now() - recorderState.silenceStartedAt;
    if (duration >= 500) recorderState.pauses.push({ start_ms: recorderState.silenceStartedAt - recorderState.startedAt, duration_ms: duration });
  }
  clearInterval(recorderState.levelTimer);
  recorderState.audioContext?.close().catch(() => {});
  recorderState.audioContext = null;
  recorderState.analyser = null;
  recorderState.silenceStartedAt = 0;
}

async function startVoiceAnswer() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setVoiceStatus("当前浏览器不支持录音，请改用 Chrome 或 Edge，或直接输入作答。");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorderState.chunks = [];
    recorderState.stream = stream;
    recorderState.shouldTranscribe = true;
    recorderState.recorder = new MediaRecorder(stream, { mimeType: getRecordingMimeType() });
    recorderState.recorder.ondataavailable = (event) => { if (event.data.size) recorderState.chunks.push(event.data); };
    recorderState.recorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      endPauseSampling();
      clearInterval(recorderState.timer);
      const blob = new Blob(recorderState.chunks, { type: recorderState.recorder.mimeType || "audio/webm" });
      if (recorderState.shouldTranscribe) await transcribeVoice(blob, Math.round((Date.now() - recorderState.startedAt) / 1000), recorderState.pauses);
    };
    recorderState.startedAt = Date.now();
    beginPauseSampling(stream);
    recorderState.recorder.start();
    recorderState.timer = setInterval(() => { $("voice-timer").textContent = formatVoiceTime((Date.now() - recorderState.startedAt) / 1000); }, 250);
    $("btn-voice-start").disabled = true;
    $("btn-voice-stop").disabled = false;
    setVoiceStatus("正在录音……完成后点击“停止并转写”。");
  } catch (error) {
    setVoiceStatus("无法访问麦克风，请检查浏览器权限，或直接输入作答。" );
  }
}

function stopVoiceAnswer() {
  if (!recorderState.recorder || recorderState.recorder.state === "inactive") return;
  $("btn-voice-stop").disabled = true;
  setVoiceStatus("正在上传并转写，请稍候……");
  recorderState.recorder.stop();
}

function stopActiveRecording(message = "") {
  if (recorderState.recorder && recorderState.recorder.state !== "inactive") {
    recorderState.shouldTranscribe = false;
    recorderState.recorder.stop();
  }
  recorderState.stream?.getTracks().forEach((track) => track.stop());
  clearInterval(recorderState.timer);
  endPauseSampling();
  $("btn-voice-start").disabled = false;
  $("btn-voice-stop").disabled = true;
  if (message) setVoiceStatus(message);
}

function applyTranscript(transcript, audioMeta) {
  const current = $("question-answer").value.trim();
  $("question-answer").value = current ? `${current}\n${transcript}` : transcript;
  state.practice.questions[state.currentIndex].audio_meta = audioMeta;
  syncQuestionDraft();
  renderQuestionJump();
  renderQuestion();
}

async function transcribeVoice(blob, durationSec, pauses) {
  if (durationSec < 1 || blob.size < 1200) {
    setVoiceStatus("录音过短或没有录到声音，请保持至少 1 秒后重试，或直接输入作答。");
    $("btn-voice-start").disabled = false;
    $("btn-voice-stop").disabled = true;
    return;
  }
  const form = new FormData();
  form.append("audio", blob, `practice-answer.${blob.type.includes("mp4") ? "mp4" : "webm"}`);
  try {
    const response = await fetch(`/api/practice/transcribe?direction=${encodeURIComponent(state.practice.direction)}`, { method: "POST", body: form });
    const text = await response.text();
    const data = JSON.parse(text);
    if (!response.ok) throw new Error(data.detail || "转写失败");
    applyTranscript(data.transcript || "", { duration_sec: durationSec, transcribed_at: new Date().toISOString(), mime_type: blob.type, pauses, pause_count: pauses.length, long_pause_count: pauses.filter((item) => item.duration_ms >= 800).length });
    setVoiceStatus(`转写完成 · ${durationSec} 秒 · 结果可继续手动修改。`);
  } catch (error) {
    setVoiceStatus(`转写失败：${error.message}。你仍可以直接输入作答。`);
  } finally {
    $("btn-voice-start").disabled = false;
    $("btn-voice-stop").disabled = true;
  }
}

$("btn-voice-start").addEventListener("click", startVoiceAnswer);
$("btn-voice-stop").addEventListener("click", stopVoiceAnswer);


function setDirection(dir) {
  state.direction = dir === "ZH-EN" ? "ZH-EN" : "EN-ZH";
  document.querySelectorAll(".dir-btn").forEach((button) => {
    button.classList.toggle("on", button.dataset.dir === state.direction);
  });
}

document.querySelectorAll(".dir-btn").forEach((button) => {
  button.addEventListener("click", () => setDirection(button.dataset.dir));
});

document.querySelectorAll(".mode-btn").forEach((button) => {
  button.addEventListener("click", () => {
    state.mode = button.dataset.mode === "custom" ? "custom" : "system";
    document.querySelectorAll(".mode-btn").forEach((item) => item.classList.toggle("on", item === button));
    $("custom-wrap").classList.toggle("hidden", state.mode !== "custom");
    $("btn-practice-start").textContent = "开始练习";
  });
});

function showView(name) {
  if (name !== "practice") stopActiveRecording("已停止当前录音。");
  document.querySelectorAll(".view").forEach((view) => view.classList.add("hidden"));
  const target = $(`view-${name}`);
  if (target) target.classList.remove("hidden");
  document.querySelectorAll(".nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  if (name === "profile") loadRecords();
  if (name === "favorites") loadFavorites();
  if (name === "magazine") loadMagazineHistory();

}

document.querySelectorAll(".nav button, [data-go-view]").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view || button.dataset.goView));
});

document.querySelectorAll("[data-export-kind]").forEach((button) => {
  button.addEventListener("click", () => {
    const { exportKind, exportFormat } = button.dataset;
    window.location.href = `/api/export/${exportKind}/${exportFormat}`;
  });
});

async function api(path, body, method = body === undefined ? "GET" : "POST") {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(text || response.statusText);
  }
  if (!response.ok) throw new Error(data.detail || text || response.statusText);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function directionLabel(direction) {
  return direction === "EN-ZH" ? "英译中" : "中译英";
}

function syncQuestionDraft() {
  if (!state.practice) return;
  const question = state.practice.questions[state.currentIndex];
  question.answer = $("question-answer").value;
  question.tone = $("question-tone").value;
  question.rhythm = $("question-rhythm").value;
}

function renderQuestion() {
  if (!state.practice) return;
  const question = state.practice.questions[state.currentIndex];
  $("practice-count").textContent = `第 ${state.currentIndex + 1} / 5 题`;
  $("practice-source-label").textContent = `${directionLabel(state.practice.direction)} · ${state.practice.source}`;
  $("progress-bar").style.width = `${((state.currentIndex + 1) / 5) * 100}%`;
  $("question-source").textContent = question.source;
  const terms = (question.term_hints || []).map((term) => `<div class="term-preview-item"><b>原文术语：${escapeHtml(term.source)}</b><span>推荐译法：${escapeHtml(term.suggestion)}</span><small>当前译法：${escapeHtml(term.current || "未作答")} · ${escapeHtml(term.reason)}</small></div>`).join("");
  $("term-preview").innerHTML = terms ? `<div class="term-preview-heading">本句术语预习（中英对照）</div>${terms}` : "";
  $("question-answer").value = question.answer || "";
  $("answer-counter").textContent = `${question.answer?.length || 0} ${state.practice.direction === "EN-ZH" ? "字" : "字符/词"}`;
  $("question-tone").value = question.tone || "待标记";
  $("question-rhythm").value = question.rhythm || "待标记";
  $("btn-prev").disabled = state.currentIndex === 0;
  $("btn-next").disabled = state.currentIndex === 4;
  $("answer-status").textContent = "";
  document.querySelectorAll(".question-chip").forEach((button, index) => {
    button.classList.toggle("active", index === state.currentIndex);
    button.classList.toggle("answered", Boolean(state.practice.questions[index].answer?.trim()));
  });
}

function renderQuestionJump() {
  $("question-jump").innerHTML = state.practice.questions
    .map((question, index) => `<button class="question-chip" data-question-index="${index}"><span>${index + 1}</span><em>${question.answer?.trim() ? "已作答" : "待作答"}</em></button>`)
    .join("");
  document.querySelectorAll(".question-chip").forEach((button) => {
    button.addEventListener("click", () => {
      stopActiveRecording("切换题目，录音已停止。");
      syncQuestionDraft();
      state.currentIndex = Number(button.dataset.questionIndex);
      renderQuestion();
    });
  });
}

function hasAllAnswers() {
  return state.practice?.questions.every((question) => question.answer?.trim());
}

async function startPractice() {
  stopActiveRecording();
  const status = $("practice-status");
  status.textContent = state.mode === "custom" ? "正在整理你的材料…" : "正在准备 5 道题…";
  $("btn-practice-start").disabled = true;
  try {
    const data = await api("/api/practice/start", {
      direction: state.direction,
      topic: $("topic").value,
      level: $("level").value,
      custom_text: state.mode === "custom" ? $("custom").value : "",
    });
    state.practice = data;
    state.review = null;
    state.sessionKey = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    state.currentIndex = 0;
    $("practice-setup").classList.add("hidden");
    $("btn-save-practice").textContent = "保存到练习记录";
    $("btn-save-practice").disabled = false;
    $("btn-go-records").classList.add("hidden");
    $("save-status").textContent = "";
    $("practice-result").classList.add("hidden");
    $("practice-workspace").classList.remove("hidden");
    renderQuestionJump();
    renderQuestion();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    $("btn-practice-start").disabled = false;
  }
}

$("custom-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    $("custom").value = await file.text();
    $("practice-status").textContent = `已载入：${file.name}`;
  } catch {
    $("practice-status").textContent = "文件读取失败，请改为直接粘贴文本。";
  }
});

$("btn-practice-start").addEventListener("click", startPractice);

$("question-answer").addEventListener("input", () => {
  syncQuestionDraft();
  $("answer-counter").textContent = `${$("question-answer").value.length} ${state.practice.direction === "EN-ZH" ? "字" : "字符/词"}`;
  renderQuestionJump();
  renderQuestion();
});
$("question-tone").addEventListener("change", syncQuestionDraft);
$("question-rhythm").addEventListener("change", syncQuestionDraft);

$("btn-prev").addEventListener("click", () => {
  stopActiveRecording("切换题目，录音已停止。");
  syncQuestionDraft();
  if (state.currentIndex > 0) state.currentIndex -= 1;
  renderQuestion();
});

$("btn-next").addEventListener("click", () => {
  stopActiveRecording("切换题目，录音已停止。");
  syncQuestionDraft();
  if (state.currentIndex < 4) state.currentIndex += 1;
  renderQuestion();
});

function suggestionRows(suggestion) {
  const item = typeof suggestion === "object" ? suggestion : { example: String(suggestion || "暂无批改") };
  const row = (label, value) => `<div class="advice-row"><b>${label}</b><p>${escapeHtml(value || "暂无")}</p></div>`;
  return `${row("原文定位", item.source_quote)}${row("问题诊断", item.diagnosis)}${row("具体改法", item.action)}${row("修改示例", item.example)}${row("原因说明", item.reason)}`;
}

function renderSentenceFeedback() {
  const item = state.review.questions[state.reviewIndex || 0];
  const feedback = item.feedback;
  const scoreRows = Object.entries(feedback.manual_mark?.scores || {}).map(([key, value]) => `<label class="score-edit"><span>${({ grammar_accuracy: "语法准确性", word_precision: "用词精准度", sentence_variety: "句式多样性", logic_coherence: "逻辑连贯度", spoken_naturalness: "口语自然度" }[key] || key)}</span><input type="number" min="0" max="100" value="${value.score}" data-score-key="${key}"><small>${escapeHtml(value.deduction)}</small></label>`).join("");
  const terms = (feedback.wording?.terms || []).map((term) => `<div class="term-item"><b>专有名词/术语</b><span>${escapeHtml(term.source)}</span><b>当前译法</b><span>${escapeHtml(term.current || "未译出")}</span><b>推荐译法</b><span>${escapeHtml(term.suggestion)}</span><small>${escapeHtml(term.reason)}</small></div>`).join("") || "<p>本句未识别到需要单独标注的专名或术语。</p>";
  const advice = (label, value) => `<div class="advice-row"><b>${label}</b><p>${escapeHtml(value || "暂无")}</p></div>`;
  const breakdown = feedback.breakdown || {};
  const example = feedback.example || {};
  const teaching = (feedback.teaching || []).map((step, index) => `<li><b>${index + 1}. ${escapeHtml(step.title)}</b><p>${escapeHtml(step.content)}</p></li>`).join("");
  $("review-sentence-count").textContent = `第 ${(state.reviewIndex || 0) + 1} / ${state.review.questions.length} 句`;
  $("review-progress-bar").style.width = `${(((state.reviewIndex || 0) + 1) / state.review.questions.length) * 100}%`;
  $("overall-score").textContent = feedback.score;
  $("overall-label").textContent = feedback.label;
  $("review-list").innerHTML = `<article class="review-item sentence-review-card"><h4>${escapeHtml(feedback.source_quote)}</h4><div class="full-analysis"><div class="analysis-block"><b>断句划分</b><p>切分前：${escapeHtml(breakdown.before || "")}</p><p>切分后：${escapeHtml(breakdown.after || "")}</p></div><div class="analysis-block"><b>问题证据与信息结构</b><p>${escapeHtml(feedback.structure?.evidence || "")}</p><p>${escapeHtml(feedback.structure?.main_clause || "")}</p><p>${escapeHtml(feedback.structure?.supporting_information || "")}</p></div><div class="analysis-block"><b>问题诊断</b><p>${escapeHtml(feedback.diagnosis)}</p></div><div class="analysis-block"><b>修改示例</b><p>原句：${escapeHtml(example.original || "")}</p><p>改后句：${escapeHtml(example.revised || "")}</p><p>改动点：${escapeHtml(example.changes || "")}</p></div><div class="analysis-block"><b>原因说明</b><p>${escapeHtml(feedback.reason)}</p></div><div class="analysis-block"><b>详细讲解与复述步骤</b><ol class="teaching-steps">${teaching || "<li>暂无详细讲解。</li>"}</ol></div><div class="analysis-block"><b>手动标记与动态评分</b><p>语气：${escapeHtml(feedback.manual_mark?.tone || "待标记")}　停顿：${escapeHtml(feedback.manual_mark?.rhythm || "待标记")}</p><div class="label-edit"><label>语气<select data-manual-tone><option ${feedback.manual_mark?.tone === "自然专业" ? "selected" : ""}>自然专业</option><option ${feedback.manual_mark?.tone === "平稳" ? "selected" : ""}>平稳</option><option ${feedback.manual_mark?.tone === "偏生硬" ? "selected" : ""}>偏生硬</option><option ${feedback.manual_mark?.tone === "待标记" ? "selected" : ""}>待标记</option></select></label><label>节奏<select data-manual-rhythm><option ${feedback.manual_mark?.rhythm === "节奏自然" ? "selected" : ""}>节奏自然</option><option ${feedback.manual_mark?.rhythm === "偶有停顿" ? "selected" : ""}>偶有停顿</option><option ${feedback.manual_mark?.rhythm === "停顿偏多" ? "selected" : ""}>停顿偏多</option><option ${feedback.manual_mark?.rhythm === "待标记" ? "selected" : ""}>待标记</option></select></label></div><div class="score-edit-grid">${scoreRows}</div></div><div class="analysis-block"><b>停顿节奏</b><p>${escapeHtml(feedback.pause_rhythm)}</p></div><div class="analysis-block"><b>用词句式</b><p>评分：${feedback.wording?.score ?? "--"}；具体问题：${escapeHtml(feedback.wording?.issue || "暂无")}</p><p>句式：${escapeHtml(feedback.wording?.sentence_note || "暂无")}</p><div class="term-list">${terms}</div></div></div><button class="ghost favorite-button" data-favorite-current="${item.id}">收藏本句作答</button></article>`;
  state.reviewViewed.add(state.reviewIndex || 0);
  $("btn-review-prev").disabled = (state.reviewIndex || 0) === 0;
  $("btn-save-practice").disabled = state.reviewViewed.size < state.review.questions.length;
  $("btn-review-next").textContent = (state.reviewIndex || 0) === state.review.questions.length - 1 ? "完成查看" : "下一句";
  document.querySelectorAll("[data-score-key]").forEach((input) => input.addEventListener("change", () => {
    const key = input.dataset.scoreKey;
    feedback.manual_mark.scores[key].score = Math.max(0, Math.min(100, Number(input.value) || 0));
    feedback.score = Math.round(Object.values(feedback.manual_mark.scores).reduce((sum, score) => sum + score.score, 0) / Object.values(feedback.manual_mark.scores).length);
    $("overall-score").textContent = feedback.score;
    state.practice.questions[state.reviewIndex || 0].manual_scores = feedback.manual_mark.scores;
  }));
  document.querySelectorAll("[data-manual-tone], [data-manual-rhythm]").forEach((select) => select.addEventListener("change", () => {
    const question = state.practice.questions[state.reviewIndex || 0];
    if (select.hasAttribute("data-manual-tone")) { feedback.manual_mark.tone = select.value; question.tone = select.value; }
    if (select.hasAttribute("data-manual-rhythm")) { feedback.manual_mark.rhythm = select.value; question.rhythm = select.value; }
  }));
  document.querySelectorAll("[data-favorite-current]").forEach((button) => button.addEventListener("click", () => {
    $("save-status").textContent = "请先保存本组练习记录，再在练习记录详情中收藏该句作答。";
  }));
}

function feedbackCard(item, index) {
  return "";
}

$("btn-review").addEventListener("click", async () => {
  syncQuestionDraft();
  if (!hasAllAnswers()) {
    $("answer-status").textContent = "还有题目未作答，请完成 5 题后再统一点评。你可以点击上方题号定位。";
    return;
  }
  $("answer-status").textContent = "正在统一点评…";
  $("btn-review").disabled = true;
  try {
    const data = await api("/api/practice/review", {
      direction: state.practice.direction,
      topic: state.practice.topic,
      level: state.practice.level,
      questions: state.practice.questions,
    });
    state.review = data;
    state.reviewIndex = 0;
    state.reviewViewed = new Set();
    state.practice.questions.forEach((question, index) => { question.feedback = data.questions[index]?.feedback || {}; });
    $("practice-workspace").classList.add("hidden");
    $("practice-result").classList.remove("hidden");
    $("review-summary").textContent = data.summary;
    renderSentenceFeedback();
  } catch (error) {
    $("answer-status").textContent = error.message;
  } finally {
    $("btn-review").disabled = false;
  }
});

$("btn-review-prev").addEventListener("click", () => {
  if (state.reviewIndex > 0) { state.reviewIndex -= 1; renderSentenceFeedback(); }
});

$("btn-review-next").addEventListener("click", () => {
  if (state.reviewIndex < state.review.questions.length - 1) {
    state.reviewIndex += 1;
    renderSentenceFeedback();
  } else {
    $("review-summary").textContent = "五句分析已全部查看。现在可以保存练习记录，或返回修改。";
  }
});


$("btn-save-practice").addEventListener("click", async () => {
  if (!state.practice || !state.review) return;
  if (state.reviewViewed.size < state.review.questions.length) {
    $("save-status").textContent = "请先完整查看全部五句分析，再保存练习记录。";
    return;
  }
  $("save-status").textContent = "正在保存练习记录…";
  $("btn-save-practice").disabled = true;
  try {
    const data = await api("/api/practice/save", {
      direction: state.practice.direction,
      topic: state.practice.topic,
      level: state.practice.level,
      source: state.practice.source,
      questions: state.practice.questions,
      review: state.review,
    });
    $("save-status").textContent = `${data.message} 已保留题目、做题记录与批改。`;
    $("btn-save-practice").textContent = "已保存";
    $("btn-save-practice").disabled = true;
    $("btn-go-records").classList.remove("hidden");
  } catch (error) {
    $("save-status").textContent = error.message;
    $("btn-save-practice").disabled = false;
  }
});

function formatTime(value) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function fullAnalysisRows(feedback) {
  const item = feedback || {};
  const example = item.example || {};
  const breakdown = item.breakdown || {};
  const teaching = (item.teaching || []).map((step, index) => `<li><b>${index + 1}. ${escapeHtml(step.title)}</b><p>${escapeHtml(step.content)}</p></li>`).join("");
  const terms = (item.wording?.terms || []).map((term) => `<div class="term-item"><b>术语</b><span>${escapeHtml(term.source)}</span><b>当前译法</b><span>${escapeHtml(term.current || "未译出")}</span><b>推荐译法</b><span>${escapeHtml(term.suggestion)}</span><small>${escapeHtml(term.reason)}</small></div>`).join("");
  return `<section class="suggestion full-record-analysis"><div class="advice-row"><b>原文定位</b><p>${escapeHtml(item.source_quote || "暂无")}</p></div><div class="advice-row"><b>断句划分</b><p>切分前：${escapeHtml(breakdown.before || "")}\n切分后：${escapeHtml(breakdown.after || "")}</p></div><div class="advice-row"><b>问题诊断</b><p>${escapeHtml(item.diagnosis || "暂无")}</p></div><div class="advice-row"><b>修改示例</b><p>原句：${escapeHtml(example.original || "")}\n改后句：${escapeHtml(example.revised || "")}\n改动点：${escapeHtml(example.changes || "")}</p></div><div class="advice-row"><b>原因说明</b><p>${escapeHtml(item.reason || "暂无")}</p></div>${teaching ? `<div class="analysis-block"><b>详细讲解</b><ol class="teaching-steps">${teaching}</ol></div>` : ""}${terms ? `<div class="analysis-block"><b>术语与译法</b><div class="term-list">${terms}</div></div>` : ""}</section>`;
}

function renderRecordDetail(record) {
  const feedbackById = Object.fromEntries((record.review.questions || []).map((item) => [item.id, item.feedback]));
  const suggestionRows = (suggestion) => { const item = typeof suggestion === "object" ? suggestion : { example: String(suggestion || "暂无批改") }; const row = (label, value) => `<div class="advice-row"><b>${label}</b><p>${escapeHtml(value || "暂无")}</p></div>`; return `${row("原文定位", item.source_quote)}${row("问题诊断", item.diagnosis)}${row("具体改法", item.action)}${row("修改示例", item.example)}${row("原因说明", item.reason)}`; };
  $("record-detail").innerHTML = `<div class="detail-heading"><div><p class="kicker">练习详情</p><h3>${escapeHtml(formatTime(record.created_at))}</h3><p class="lead">${escapeHtml(directionLabel(record.direction))} · ${escapeHtml(record.source)} · 总分 ${record.review.overall_score}（${escapeHtml(record.review.overall_label)}）</p></div><button class="ghost" id="btn-close-detail">收起详情</button></div><div class="detail-questions">${record.questions.map((question, index) => { const feedback = feedbackById[question.id] || {}; const metric = (name, item) => `<li><span>${name}</span><b>${item?.score ?? "--"}</b><em>${escapeHtml(item?.comment || "暂无评语")}</em></li>`; return `<article class="detail-question"><h4>第 ${index + 1} 题 <span>${feedback.score ?? "--"} · ${escapeHtml(feedback.label || "")}</span></h4><p><b>题目</b>${escapeHtml(question.source)}</p><p><b>我的作答</b>${escapeHtml(question.answer)}</p><div class="detail-metrics"><ul>${metric("内容准确性", feedback.content_accuracy)}${metric("表达流畅度", feedback.expression_fluency)}${metric("语气（手动）", feedback.tone)}${metric("停顿 / 节奏（手动）", feedback.rhythm)}${metric("用词与句式", feedback.wording_structure)}</ul></div>${fullAnalysisRows(feedback)}<button class="ghost" data-record-export="${record.id}" data-export-format="docx">导出 Word</button><button class="ghost" data-record-export="${record.id}" data-export-format="pdf">导出 PDF</button><button class="ghost favorite-button" data-favorite-record="${record.id}" data-favorite-question="${question.id}">收藏并写笔记</button></article>`; }).join("")}</div>`;
  $("record-detail").classList.remove("hidden");
  $("btn-close-detail").addEventListener("click", () => $("record-detail").classList.add("hidden"));
  document.querySelectorAll("[data-record-export]").forEach((button) => button.addEventListener("click", () => {
    window.location.href = `/api/export/record/${button.dataset.exportFormat}?record_id=${encodeURIComponent(button.dataset.recordExport)}`;
  }));
  document.querySelectorAll("[data-favorite-record]").forEach((button) => button.addEventListener("click", async () => {
    const note = prompt("添加收藏笔记（可留空，之后可编辑）：", "");
    if (note === null) return;
    try { await api("/api/practice/favorites", { record_id: button.dataset.favoriteRecord, question_id: button.dataset.favoriteQuestion, note }); button.textContent = "已收藏"; button.disabled = true; } catch (error) { alert(error.message); }
  }));
  $("record-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderMagazine(article) {
  const elements = Object.entries(article.elements || {}).map(([key, value]) => `<div class="magazine-element"><b>${escapeHtml(key)}</b><span>${escapeHtml(Array.isArray(value) ? value.join("、") : value)}</span></div>`).join("");
  const questions = (article.questions || []).map((question, index) => `<article class="magazine-question"><h4>${index + 1}. ${escapeHtml(question.type || "理解题")} · ${escapeHtml(question.difficulty || article.level || "中级")}</h4><p><b>题目</b>${escapeHtml(question.prompt || "")}</p><p><b>对应段落</b>${escapeHtml(String(question.paragraph || ""))}</p><details><summary>参考答案与评分要点</summary><p>${escapeHtml(question.answer || "")}</p><p>${escapeHtml(Array.isArray(question.scoring_points) ? question.scoring_points.join("；") : question.scoring_points || "")}</p><p>${escapeHtml(question.explanation || "")}</p></details></article>`).join("");
  $("magazine-result").innerHTML = `<div class="magazine-heading"><p class="kicker">${escapeHtml(article.elements?.["刊物风格"] || article.style || "英语刊物")}</p><h3>${escapeHtml(article.title)}</h3><p class="magazine-standfirst">${escapeHtml(article.standfirst)}</p><p class="meta-row">${article.word_count || 0} 词 · 建议阅读 ${escapeHtml(String(article.reading_time || ""))}</p></div><div class="magazine-body">${(article.body || []).map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join("")}</div><p class="magazine-kicker">${escapeHtml(article.kicker)}</p><h4>本文元素拆解</h4><div class="magazine-elements">${elements}</div><h4>配套题目（${(article.questions || []).length}）</h4><div class="magazine-questions">${questions}</div><details class="magazine-check"><summary>查看去重自检报告</summary><pre>${escapeHtml(JSON.stringify(article.self_check || {}, null, 2))}</pre></details>`;
  $("magazine-result").classList.remove("hidden");
}

async function loadMagazineHistory() {
  try { const data = await api("/api/magazine/history"); $("magazine-status").textContent = data.articles?.length ? `历史文章 ${data.articles.length} 篇` : "尚无历史文章"; } catch (error) { $("magazine-status").textContent = error.message; }
}

$("btn-magazine-generate").addEventListener("click", async () => {
  $("btn-magazine-generate").disabled = true; $("magazine-status").textContent = "正在生成刊物文章、配套题目并执行去重检查……";
  try { const article = await api("/api/magazine/generate", { direction: state.direction, topic: $("magazine-topic").value, level: $("magazine-level").value, style: $("magazine-style").value }); renderMagazine(article); $("magazine-status").textContent = `生成完成，已通过 ${article.self_check?.attempts || 1} 次校验。`; } catch (error) { $("magazine-status").textContent = error.message; } finally { $("btn-magazine-generate").disabled = false; }
});

async function loadRecords() {
  try {
    const data = await api("/api/practice/records");
    const records = data.records || [];
    $("records-empty").classList.toggle("hidden", records.length > 0);
    $("btn-clear-records").disabled = records.length === 0;
    $("records-list").innerHTML = records.map((record) => `<article class="record-row"><div><p class="record-time">${escapeHtml(formatTime(record.created_at))}</p><h3>${escapeHtml(record.source || "练习题组")}</h3><p>${escapeHtml(directionLabel(record.direction))} · ${record.question_count} 题 · 保留题目、做题记录与批改</p></div><div class="record-score"><strong>${record.overall_score}</strong><span>${escapeHtml(record.overall_label)}</span></div><div class="record-row-actions"><button class="ghost" data-record-detail="${record.id}">查看详情</button><button class="text-danger" data-record-delete="${record.id}">删除</button></div></article>`).join("");
    document.querySelectorAll("[data-record-detail]").forEach((button) => button.addEventListener("click", async () => {
      try { renderRecordDetail(await api(`/api/practice/records/${button.dataset.recordDetail}`)); } catch (error) { alert(error.message); }
    }));
    document.querySelectorAll("[data-record-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("确认删除这条练习记录吗？此操作无法撤销。")) return;
      try { await api(`/api/practice/records/${button.dataset.recordDelete}`, undefined, "DELETE"); $("record-detail").classList.add("hidden"); loadRecords(); } catch (error) { alert(error.message); }
    }));
  } catch (error) {
    $("records-list").innerHTML = `<p class="status">${escapeHtml(error.message)}</p>`;
  }
}

async function loadFavorites() {
  try {
    const data = await api("/api/practice/favorites");
    const favorites = data.favorites || [];
    $("favorites-empty").classList.toggle("hidden", favorites.length > 0);
    $("favorites-list").innerHTML = favorites.map((favorite) => `<article class="favorite-card"><p class="record-time">${escapeHtml(formatTime(favorite.created_at))}</p><h3>${escapeHtml(favorite.source)}</h3><p class="favorite-answer"><b>我的作答</b>${escapeHtml(favorite.answer)}</p>${fullAnalysisRows(favorite.feedback)}<label>我的笔记<textarea data-favorite-note="${favorite.id}" rows="3">${escapeHtml(favorite.note || "")}</textarea></label><div class="actions"><button class="ghost" data-favorite-save="${favorite.id}">保存笔记</button><button class="text-danger" data-favorite-delete="${favorite.id}">取消收藏</button></div></article>`).join("");
    document.querySelectorAll("[data-favorite-save]").forEach((button) => button.addEventListener("click", async () => {
      const note = document.querySelector(`[data-favorite-note="${button.dataset.favoriteSave}"]`).value;
      try { await api(`/api/practice/favorites/${button.dataset.favoriteSave}`, { record_id: "", question_id: "", note }, "PUT"); button.textContent = "已保存"; } catch (error) { alert(error.message); }
    }));
    document.querySelectorAll("[data-favorite-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("确认取消收藏吗？")) return;
      try { await api(`/api/practice/favorites/${button.dataset.favoriteDelete}`, undefined, "DELETE"); loadFavorites(); } catch (error) { alert(error.message); }
    }));
  } catch (error) {
    $("favorites-list").innerHTML = `<p class="status">${escapeHtml(error.message)}</p>`;
  }
}

async function loadReviewHistory() {
  try {
    const data = await api("/api/practice/professional-reviews");
    const reviews = data.reviews || [];
    $("review-history-list").innerHTML = reviews.length ? reviews.map((review) => `<article class="history-item"><div><p class="record-time">${escapeHtml(formatTime(review.created_at))}</p><h4>最近 ${review.count} 次练习 · 平均得分 ${review.overall_score}</h4><p>${escapeHtml(review.conclusion || "")}</p></div><button class="text-danger" data-review-history-delete="${review.id}">删除</button></article>`).join("") : `<p class="history-empty">暂无已保存的复盘。</p>`;
    document.querySelectorAll("[data-review-history-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("确认删除这份历史复盘吗？")) return;
      try { await api(`/api/practice/professional-reviews/${button.dataset.reviewHistoryDelete}`, undefined, "DELETE"); loadReviewHistory(); } catch (error) { alert(error.message); }
    }));
  } catch (error) {
    $("review-history-list").innerHTML = `<p class="status">${escapeHtml(error.message)}</p>`;
  }
}

$("btn-clear-records").addEventListener("click", async () => {
  if (!confirm("确认清空全部练习记录吗？题目、做题记录和批改将一并删除，且无法恢复。")) return;
  try { await api("/api/practice/records", undefined, "DELETE"); $("record-detail").classList.add("hidden"); loadRecords(); } catch (error) { alert(error.message); }
});

let latestProfessionalReview = null;

function renderProfessionalReview(data) {
  const trend = data.score_trend?.join(" → ") || "暂无趋势";
  const plan = data.focus_plan || {};
  const cases = (item) => (item.cases || []).map((entry) => `<div class="review-case"><b>${escapeHtml(entry.index)}</b><p><strong>原音转写</strong>${escapeHtml(entry.original_transcript)}</p><p><strong>原文命题</strong>${escapeHtml(entry.source)}</p><p><strong>问题定位</strong>${escapeHtml(entry.problem)}</p><p><strong>标准改法</strong>${escapeHtml(entry.reference)}</p><p><strong>原因剖析</strong>${escapeHtml(entry.reason)}</p></div>`).join("");
  const dimensions = (data.weaknesses || []).map((item) => `<article class="review-dimension"><h3>${escapeHtml(item.name)} <span>${item.han?.label || ""} · ${item.average}/100</span></h3><p>${escapeHtml(item.diagnosis || item.advice || "")}</p>${cases(item)}</article>`).join("");
  const errors = (data.top_errors || []).map((item) => `<article class="review-case"><b>${escapeHtml(item.index)}</b><p><strong>原文</strong>${escapeHtml(item.source)}</p><p><strong>原音转写</strong>${escapeHtml(item.answer)}</p><p><strong>问题</strong>${escapeHtml(item.problem)}</p></article>`).join("");
  return `<div class="professional-result"><p><b>复盘样本</b> 最近 ${data.count} 次练习 · 平均得分 ${data.overall_score}</p><p><b>得分趋势</b> ${escapeHtml(trend)}</p><article class="focus-plan"><h3>本周期重点：${escapeHtml(plan.name || "")} <span>${plan.days || 7} 天计划</span></h3><p><strong>选材</strong>${escapeHtml(plan.material || "")}</p><p><strong>步骤</strong>${escapeHtml(plan.steps || "")}</p><p><strong>量化标准</strong>${escapeHtml(plan.metric || "")}</p></article><p>${escapeHtml(data.evolution || "")}</p><p>${escapeHtml(data.conclusion || "")}</p>${dimensions}<section><h3>常犯错误 Top 3</h3>${errors}</section></div>`;
}

$("btn-generate-review").addEventListener("click", async () => {
  $("professional-review-status").textContent = "正在基于最近练习生成复盘…";
  try {
    const data = await api("/api/practice/professional-review", { count: Number($("review-count").value) });
    latestProfessionalReview = data;
    $("btn-save-professional-review").disabled = false;
    $("professional-review-status").textContent = "专业复盘已生成。";
    $("professional-review-result").innerHTML = renderProfessionalReview(data);
  } catch (error) {
    $("professional-review-status").textContent = error.message;
    $("professional-review-result").innerHTML = "";
  }
});

$("btn-save-professional-review").addEventListener("click", async () => {
  if (!latestProfessionalReview) return;
  $("btn-save-professional-review").disabled = true;
  try {
    await api("/api/practice/professional-review", { count: latestProfessionalReview.count, save: true });
    $("professional-review-status").textContent = "本次复盘已保存，可用于后续对照。";
    $("btn-save-professional-review").textContent = "已保存";
  } catch (error) {
    $("professional-review-status").textContent = error.message;
    $("btn-save-professional-review").disabled = false;
  }
});

$("btn-edit-practice").addEventListener("click", () => {
  $("practice-result").classList.add("hidden");
  $("practice-workspace").classList.remove("hidden");
  renderQuestionJump();
  renderQuestion();
});

window.addEventListener("beforeunload", (event) => {
  stopActiveRecording();
  if (state.practice && !$("btn-save-practice").disabled) {
    event.preventDefault();
    event.returnValue = "当前练习尚未保存，确定离开吗？";
  }
});

$("btn-practice-again").addEventListener("click", () => {
  stopActiveRecording("已停止当前录音。");
  state.practice = null;
  state.review = null;
  state.sessionKey = null;
  state.currentIndex = 0;
  $("practice-result").classList.add("hidden");
  $("practice-workspace").classList.add("hidden");
  $("btn-go-records").classList.add("hidden");
  $("practice-setup").classList.remove("hidden");
  $("practice-status").textContent = "";
});

async function boot() {
  try {
    const meta = await api("/api/meta");
    $("date-line").textContent = `${meta.today} · 默认方向：${meta.default_direction_label}`;
    setDirection(meta.default_direction);
    setVoiceStatus(meta.asr_ready ? "语音转写已就绪。" : "语音转写暂不可用，仍可手动输入作答。");
  } catch (error) {
    $("practice-status").textContent = `服务连接异常：${error.message}`;
  }
}

boot();

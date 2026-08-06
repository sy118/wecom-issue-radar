import { describe, expect, it } from "vitest";
import {
  imageStatusCopy,
  workAnswerCopy,
  workFromWire,
  workStageStepCopy,
  workStageTimeline,
  workStatusCopy,
} from "./GroupReplyPage";

describe("group reply work presentation", () => {
  it("preserves timing, image, and folded-history metadata from runtime work records", () => {
    const work = workFromWire({
      id: "work-1",
      status: "waiting_for_human_reply",
      detectedAt: "2026-07-31T08:00:01Z",
      sourceDelaySeconds: 4.25,
      mergeDueAt: "2026-07-31T08:00:21Z",
      completedAt: "2026-07-31T08:02:31Z",
      humanWaitDueAt: "2026-07-31T08:02:21Z",
      imageCount: 2,
      imageAvailableCount: 1,
      imageUnavailableCount: 1,
      imageStatus: "partial",
      duplicateCount: 3,
    });

    expect(work).toMatchObject({
      detectedAt: "2026-07-31T08:00:01Z",
      sourceDelaySeconds: 4.25,
      mergeDueAt: "2026-07-31T08:00:21Z",
      completedAt: "2026-07-31T08:02:31Z",
      humanWaitDueAt: "2026-07-31T08:02:21Z",
      imageCount: 2,
      imageAvailableCount: 1,
      imageUnavailableCount: 1,
      imageStatus: "partial",
      duplicateCount: 3,
    });
  });

  it("reserves the generating copy for active work and explains empty terminal outcomes", () => {
    expect(workAnswerCopy(workFromWire({ status: "retrieving" }))).toBe("回答仍在生成中");
    expect(workAnswerCopy(workFromWire({
      status: "ignored_unsupported",
      imageStatus: "unavailable",
    }))).toBe("这类消息暂不支持处理，本次未生成回答。");
    expect(workAnswerCopy(workFromWire({
      status: "skipped_image_unavailable",
      imageStatus: "unavailable",
    }))).toBe("图片无法读取，本次未生成回答。");
    expect(workAnswerCopy(workFromWire({ status: "answered_by_human" })))
      .toBe("群友已在等待期内回复，本次无需生成回答。");
    expect(workAnswerCopy(workFromWire({
      status: "skipped_review_failed",
      answer: "这段草稿没有通过证据审核，不得展示。",
    })))
      .toBe("独立审核未通过，本次未生成回答。");
    expect(workAnswerCopy(workFromWire({ status: "failed", imageStatus: "unsupported" })))
      .toBe("当前模型不支持图片识别，本次未生成回答。");
    expect(workAnswerCopy(workFromWire({
      status: "ignored_non_question",
      imageStatus: "unavailable",
    }))).toBe("这条消息未被识别为问题，本次未生成回答。");
    expect(workAnswerCopy(workFromWire({
      status: "failed",
      imageStatus: "unavailable",
      error: { code: "MCP_SESSION_INTERRUPTED", stage: "retrieving" },
    }))).toBe("MCP 工具请求发出后会话中断；为避免重复执行，系统未自动重放。");
    expect(workAnswerCopy(workFromWire({
      status: "failed",
      error: { code: "MCP_SESSION_INTERRUPTED", stage: "retrieving" },
    }))).toBe("MCP 工具请求发出后会话中断；为避免重复执行，系统未自动重放。");
    expect(workAnswerCopy(workFromWire({ status: "closed", reason: "" })))
      .toBe("本次未生成回答。");
    expect(workAnswerCopy(workFromWire({ status: "sent", answer: "已确认原因。" })))
      .toBe("已确认原因。");
    expect(workAnswerCopy(workFromWire({
      status: "sent",
      answer: "已按文字内容确认原因。",
      imageStatus: "unavailable",
    }))).toBe("已按文字内容确认原因。");
    expect(workAnswerCopy(workFromWire({
      status: "retrieving",
      imageStatus: "unavailable",
    }))).toBe("回答仍在生成中");
  });

  it("uses user-facing Chinese labels for active runtime stages", () => {
    expect(workStatusCopy(workFromWire({ status: "collecting" }))).toBe("等待连续补充");
    expect(workStatusCopy(workFromWire({ status: "waiting_for_human_reply" })))
      .toBe("等待群友回复");
    expect(workStatusCopy(workFromWire({ status: "queued_retrieval" })))
      .toBe("等待 MCP 检索");
    expect(workStatusCopy(workFromWire({ status: "retrieving" }))).toBe("MCP 检索中");
    expect(workStatusCopy(workFromWire({ status: "ready_to_send" }))).toBe("准备发送");
    expect(workStatusCopy(workFromWire({ status: "needs_image" }))).toBe("需要图片");
  });

  it("shows image-blocked work as actionable instead of a successful pipeline", () => {
    const item = workFromWire({
      status: "needs_image",
      imageStatus: "unavailable",
      imageCount: 3,
      imageWaitDueAt: "2026-08-03T08:00:45Z",
      error: { code: "IMAGE_FILE_MISSING", stage: "waiting_for_image" },
    });

    expect(item.status).toBe("pending");
    expect(workStatusCopy(item)).toBe("需要图片");
    expect(workStageTimeline(item).map((step) => step.state))
      .toEqual(["complete", "complete", "failed", "skipped", "skipped"]);
    expect(workAnswerCopy(item)).toBe("图片尚未写入本机缓存");
  });

  it("shows the WeCom image-download wait and its deadline for either wire shape", () => {
    const deadline = "2026-08-03T08:00:45Z";
    const waitingItems = [
      workFromWire({
        status: "waiting_for_image",
        imageWaitDueAt: deadline,
        imageStatus: "resolving",
      }),
      workFromWire({
        status: "collecting",
        pendingReason: "waiting_for_image",
        image_wait_due_at: deadline,
        imageStatus: "resolving",
      }),
    ];

    for (const item of waitingItems) {
      expect(item.status).toBe("waiting");
      expect(item.imageWaitDueAt).toBe(deadline);
      expect(workStatusCopy(item)).toBe("等待图片写入本机缓存");
      const imageWaitStep = workStageTimeline(item)
        .find((step) => step.label === "等待图片写入本机缓存");
      expect(imageWaitStep).toMatchObject({ state: "current", deadline });
      expect(workStageStepCopy(imageWaitStep!)).toContain("截止");
    }
  });

  it("explains that an image-download timeout means the file never reached this computer", () => {
    const timedOut = workFromWire({
      status: "skipped_image_unavailable",
      pendingReason: "image_download_timeout",
      imageWaitDueAt: "2026-08-03T08:00:45Z",
      completedAt: "2026-08-03T08:00:45Z",
      imageStatus: "unavailable",
      error: {
        code: "IMAGE_FILE_MISSING",
        stage: "waiting_for_image",
      },
    });

    expect(workAnswerCopy(timedOut)).toBe("图片尚未写入本机缓存");
  });

  it("shows the four processing stages with the active gate and its deadline", () => {
    const collecting = workFromWire({
      status: "collecting",
      detectedAt: "2026-07-31T08:00:01Z",
      mergeDueAt: "2026-07-31T08:00:21Z",
    });
    expect(workStageTimeline(collecting).map(({ label, state, deadline }) => ({ label, state, deadline })))
      .toEqual([
        { label: "发现消息", state: "complete", deadline: "2026-07-31T08:00:01Z" },
        { label: "等待连续补充", state: "current", deadline: "2026-07-31T08:00:21Z" },
        { label: "等待群友", state: "upcoming", deadline: undefined },
        { label: "MCP 检索", state: "upcoming", deadline: undefined },
      ]);

    const retrieving = workStageTimeline(workFromWire({
      status: "retrieving",
      humanWaitDueAt: "2026-07-31T08:02:21Z",
    }));
    expect(retrieving.map((step) => step.state)).toEqual(["complete", "complete", "complete", "current"]);

    for (const status of ["ready_to_send", "sending"]) {
      expect(workStageTimeline(workFromWire({ status })).map((step) => step.state))
        .toEqual(["complete", "complete", "complete", "complete"]);
    }

    const unreadableImage = workStageTimeline(workFromWire({
      status: "skipped_image_unavailable",
      mergeDueAt: "2026-07-31T08:00:21Z",
    }));
    expect(unreadableImage.map((step) => step.state))
      .toEqual(["complete", "skipped", "skipped", "skipped"]);
    expect(unreadableImage[1].deadline).toBe("2026-07-31T08:00:21Z");
    expect(workStageStepCopy(unreadableImage[1])).toBe("无需执行");
    expect(workStageStepCopy(workStageTimeline(collecting)[1])).toContain("截止");
    const waitingForHuman = workStageTimeline(workFromWire({
      status: "waiting_for_human_reply",
      humanWaitDueAt: "2026-07-31T08:02:21Z",
    }));
    expect(waitingForHuman.map((step) => step.state))
      .toEqual(["complete", "complete", "current", "upcoming"]);
    expect(workStageStepCopy(waitingForHuman[2])).toContain("截止");
    const unreadableImageAfterMerge = workStageTimeline(workFromWire({
      status: "skipped_image_unavailable",
      mergeDueAt: "2026-07-31T08:00:21Z",
      completedAt: "2026-07-31T08:00:21Z",
    }));
    expect(unreadableImageAfterMerge.map((step) => step.state))
      .toEqual(["complete", "complete", "skipped", "skipped"]);
    const nonQuestion = workStageTimeline(workFromWire({ status: "ignored_non_question" }));
    expect(nonQuestion.map((step) => step.state))
      .toEqual(["complete", "complete", "skipped", "skipped"]);
    const imageClassificationFailure = workStageTimeline(workFromWire({
      status: "failed",
      imageStatus: "unavailable",
      error: { code: "IMAGE_UNREADABLE", stage: "collecting" },
    }));
    expect(imageClassificationFailure.map((step) => step.state))
      .toEqual(["complete", "complete", "skipped", "skipped"]);

    const retrievalFailure = workStageTimeline(workFromWire({
      status: "failed",
      error: { code: "MCP_SESSION_INTERRUPTED", stage: "retrieving" },
    }));
    expect(retrievalFailure.map((step) => step.state))
      .toEqual(["complete", "complete", "complete", "failed"]);

    const collectingInterruptedByConfiguration = workStageTimeline(workFromWire({
      status: "closed_configuration_changed",
      error: { code: "LISTENER_CONFIGURATION_CHANGED", stage: "collecting" },
    }));
    expect(collectingInterruptedByConfiguration.map((step) => step.state))
      .toEqual(["complete", "skipped", "skipped", "skipped"]);

    const unsupported = workStageTimeline(workFromWire({ status: "ignored_unsupported" }));
    expect(unsupported.map((step) => step.state))
      .toEqual(["complete", "skipped", "skipped", "skipped"]);
  });

  it("explains whether attached images were actually available to the answer", () => {
    expect(workFromWire({ imageStatus: "resolving" }).imageStatus).toBe("resolving");
    expect(imageStatusCopy("resolving", 3)).toBe("等待图片写入本机缓存");
    expect(imageStatusCopy("processed", 2)).toBe("2 张图片已识别并用于回答");
    expect(imageStatusCopy("ready", 1)).toBe("1 张图片已读取并提供给模型");
    expect(imageStatusCopy("partial", 3, 1, 2))
      .toBe("1 张已读取，2 张无法读取；回答仅参考已读取图片");
    expect(imageStatusCopy("unavailable", 3)).toBe("3 张图片无法读取");
    expect(imageStatusCopy("unsupported", 1)).toBe("当前模型不支持识别这张图片");
    expect(imageStatusCopy("none", 0)).toBe("未附带图片");
  });
});

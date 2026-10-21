//! Standalone Windows UI Automation helper that fills a draft into the
//! *personal WeChat* message input box.
//!
//! The Electron main process spawns the compiled `wechat-input-helper.exe`
//! once per fill request and exchanges one JSON line over stdin/stdout (see
//! `README.md`). There is no Tauri dependency and no other I/O.
//!
//! Safety boundaries (do not weaken):
//!
//! - The only write operation is `UIValuePattern::set_value`. This crate never
//!   clicks a send button, never presses Enter, and never injects keyboard or
//!   mouse input of its own.
//! - A fill requires all of the following, checked in order: the personal
//!   WeChat main window is unique (class `Qt51514QWindowIcon`, name `微信`);
//!   within 8 seconds the focused element belongs to that process and window;
//!   the element is an enabled, on-screen `Edit`/`Document` control positioned
//!   inside the message-editor area; the target chat title is visible in the
//!   window header; the input box is neither read-only nor non-empty; and the
//!   focus plus chat title are confirmed once more immediately before writing.
//! - Blank chat title or blank draft text is rejected before UI Automation is
//!   initialized.
//! - Any validation failure writes nothing and reports a fixed Chinese reason
//!   so the caller can ask the user to copy the draft instead.

#![forbid(unsafe_code)]

use serde::Serialize;
use std::time::Duration;

/// Maximum accepted stdin size. Drafts are small; this bounds memory usage.
pub const MAX_INPUT_BYTES: usize = 1024 * 1024;

/// Time budget for the whole request, including process start-up and validation.
pub const TOTAL_BUDGET: Duration = Duration::from_secs(12);

/// Focus-confirmation timeout, unchanged from the original Tauri command.
pub const FOCUS_CONFIRM_TIMEOUT: Duration = Duration::from_secs(8);

#[cfg(windows)]
const POLL_INTERVAL: Duration = Duration::from_millis(120);

/// Success payload: exactly one JSON line on stdout, exit code 0.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FillResult {
    pub filled: bool,
    pub chat_title: String,
    pub characters: usize,
}

/// Failure payload: exactly one JSON line on stdout, exit code 1.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FillFailure {
    pub filled: bool,
    pub error: String,
}

impl FillFailure {
    pub fn new(error: impl Into<String>) -> Self {
        Self {
            filled: false,
            error: error.into(),
        }
    }
}

/// Validates caller-provided values without touching WeChat.
pub fn validate_request(chat_title: &str, text: &str) -> Result<(), String> {
    if chat_title.trim().is_empty() || text.trim().is_empty() {
        return Err("聊天名称和草稿内容不能为空。".to_string());
    }
    Ok(())
}

/// Parses the single JSON request line.
///
/// A UTF-8 BOM and surrounding whitespace are tolerated. Unknown fields are
/// ignored so the Electron side can add metadata without breaking the helper.
pub fn parse_request_line(line: &str) -> Result<(String, String), String> {
    let line = line.trim().trim_start_matches('\u{feff}').trim();
    if line.is_empty() {
        return Err("标准输入为空，请写入一行 JSON。".to_string());
    }
    let value: serde_json::Value = serde_json::from_str(line)
        .map_err(|error| format!("输入不是有效 JSON：{error}"))?;
    let object = value
        .as_object()
        .ok_or_else(|| "输入必须是 JSON 对象。".to_string())?;
    let chat_title = object
        .get("chatTitle")
        .and_then(|value| value.as_str())
        .ok_or_else(|| "输入缺少字符串字段 chatTitle。".to_string())?;
    let text = object
        .get("text")
        .and_then(|value| value.as_str())
        .ok_or_else(|| "输入缺少字符串字段 text。".to_string())?;
    Ok((chat_title.to_string(), text.to_string()))
}

/// Fills `text` into the personal WeChat input box of `chat_title`.
///
/// Returns only after all validations pass. Never sends the message.
#[cfg(windows)]
pub fn fill_wechat_input(chat_title: &str, text: &str) -> Result<FillResult, String> {
    use std::thread;
    use std::time::Instant;
    use uiautomation::patterns::UIValuePattern;
    use uiautomation::types::ControlType;
    use uiautomation::UIAutomation;

    validate_request(chat_title, text)?;
    let chat_title = chat_title.trim();
    let started = Instant::now();

    let automation = UIAutomation::new()
        .map_err(|_| "无法初始化 Windows UI Automation。")?;
    let windows = automation
        .create_matcher()
        .control_type(ControlType::Window)
        .classname("Qt51514QWindowIcon")
        .match_name("微信")
        .depth(3)
        .timeout(500)
        .find_all()
        .map_err(|_| "没有找到个人微信主窗口。")?;
    if windows.len() != 1 {
        return Err("无法唯一确定个人微信主窗口，已拒绝填入。".to_string());
    }
    let window = &windows[0];
    let walker = automation
        .create_tree_walker()
        .map_err(|_| "无法验证当前焦点是否属于微信主窗口。")?;
    let window_process = window
        .get_process_id()
        .map_err(|_| "无法验证个人微信进程。")?;
    let window_rect = window
        .get_bounding_rectangle()
        .map_err(|_| "无法读取个人微信窗口位置。")?;
    let focus_deadline = Instant::now() + FOCUS_CONFIRM_TIMEOUT;

    while Instant::now() < focus_deadline {
        if started.elapsed() > TOTAL_BUDGET {
            let message = "填入超时（超过 12 秒），已中止且未发送任何消息；请使用复制。";
            return Err(message.to_string());
        }
        let Ok(input) = automation.get_focused_element() else {
            thread::sleep(POLL_INTERVAL);
            continue;
        };
        if input.get_process_id().ok() != Some(window_process)
            || !is_descendant(&automation, &walker, &input, window)
            || !matches!(
                input.get_control_type(),
                Ok(ControlType::Edit | ControlType::Document)
            )
            || !input.is_enabled().unwrap_or(false)
            || input.is_offscreen().unwrap_or(true)
            || !is_probable_message_editor(&input, &window_rect)
            || !chat_title_is_in_header(&automation, window, chat_title, &window_rect)
        {
            thread::sleep(POLL_INTERVAL);
            continue;
        }
        let pattern = input
            .get_pattern::<UIValuePattern>()
            .map_err(|_| "当前焦点控件不是可安全填值的微信输入框；请使用复制。")?;
        if pattern.is_readonly().unwrap_or(true) {
            return Err("当前微信输入框是只读状态，已拒绝填入。".to_string());
        }
        if !pattern.get_value().unwrap_or_default().trim().is_empty() {
            return Err("微信输入框中已有内容，为避免覆盖已拒绝填入。".to_string());
        }
        let focused_again = automation
            .get_focused_element()
            .map_err(|_| "填入前无法再次验证微信焦点。")?;
        if !automation
            .compare_elements(&focused_again, &input)
            .unwrap_or(false)
            || !chat_title_is_in_header(&automation, window, chat_title, &window_rect)
        {
            thread::sleep(POLL_INTERVAL);
            continue;
        }
        pattern
            .set_value(text)
            .map_err(|_| "微信拒绝了 UI Automation 填值；请使用复制。")?;
        return Ok(FillResult {
            filled: true,
            chat_title: chat_title.to_string(),
            characters: text.chars().count(),
        });
    }
    Err("未在 8 秒内确认目标微信聊天和空白输入框，已拒绝填入；请使用复制。".to_string())
}

/// Non-Windows stub: validates the request, then fails without touching anything.
#[cfg(not(windows))]
pub fn fill_wechat_input(chat_title: &str, text: &str) -> Result<FillResult, String> {
    validate_request(chat_title, text)?;
    Err("填入微信仅支持 Windows。".to_string())
}

#[cfg(windows)]
fn is_descendant(
    automation: &uiautomation::UIAutomation,
    walker: &uiautomation::UITreeWalker,
    element: &uiautomation::UIElement,
    ancestor: &uiautomation::UIElement,
) -> bool {
    let mut current = element.clone();
    for _ in 0..32 {
        if automation
            .compare_elements(&current, ancestor)
            .unwrap_or(false)
        {
            return true;
        }
        let Ok(parent) = walker.get_parent(&current) else {
            return false;
        };
        current = parent;
    }
    false
}

#[cfg(windows)]
fn is_probable_message_editor(
    element: &uiautomation::UIElement,
    window: &uiautomation::types::Rect,
) -> bool {
    let Ok(rect) = element.get_bounding_rectangle() else {
        return false;
    };
    let width = window.get_right() - window.get_left();
    let height = window.get_bottom() - window.get_top();
    let element_width = rect.get_right() - rect.get_left();
    let element_height = rect.get_bottom() - rect.get_top();
    width > 0
        && height > 0
        && element_width >= 160
        && element_height >= 36
        && rect.get_left() >= window.get_left() + width / 4
        && rect.get_top() >= window.get_top() + height / 2
        && rect.get_right() <= window.get_right() + 2
        && rect.get_bottom() <= window.get_bottom() + 2
}

#[cfg(windows)]
fn chat_title_is_in_header(
    automation: &uiautomation::UIAutomation,
    window: &uiautomation::UIElement,
    chat_title: &str,
    window_rect: &uiautomation::types::Rect,
) -> bool {
    let Ok(matches) = automation
        .create_matcher()
        .from_ref(window)
        .name(chat_title)
        .depth(18)
        .timeout(0)
        .find_all()
    else {
        return false;
    };
    let width = window_rect.get_right() - window_rect.get_left();
    let height = window_rect.get_bottom() - window_rect.get_top();
    matches.iter().any(|element| {
        let Ok(rect) = element.get_bounding_rectangle() else {
            return false;
        };
        !element.is_offscreen().unwrap_or(true)
            && rect.get_left() + (rect.get_right() - rect.get_left()) / 2
                >= window_rect.get_left() + width / 3
            && rect.get_top() >= window_rect.get_top()
            && rect.get_top() <= window_rect.get_top() + height / 4
    })
}

#[cfg(test)]
mod tests {
    use super::{fill_wechat_input, parse_request_line, validate_request, FillFailure, FillResult};

    #[test]
    fn rejects_blank_chat_title_before_touching_wechat() {
        let error = fill_wechat_input("   ", "草稿").unwrap_err();
        assert_eq!(error, "聊天名称和草稿内容不能为空。");
    }

    #[test]
    fn rejects_blank_text_before_touching_wechat() {
        let error = fill_wechat_input("目标聊天", " \r\n ").unwrap_err();
        assert_eq!(error, "聊天名称和草稿内容不能为空。");
    }

    #[test]
    fn reports_blank_values_without_touching_wechat() {
        assert!(validate_request("", "草稿").is_err());
        assert!(validate_request("目标聊天", "").is_err());
        assert!(validate_request("目标聊天", "草稿").is_ok());
    }

    #[test]
    fn parses_the_contract_request() {
        let line = r#"{"chatTitle":"目标聊天","text":"草稿内容"}"#;
        let parsed = parse_request_line(line).expect("should parse");
        assert_eq!(parsed, ("目标聊天".to_string(), "草稿内容".to_string()));
    }

    #[test]
    fn tolerates_bom_crlf_and_unknown_fields() {
        let line = "\u{feff}{\"chatTitle\":\"群\",\"text\":\"草稿\",\"note\":\"extra\"}\r\n";
        let parsed = parse_request_line(line).expect("should parse");
        assert_eq!(parsed, ("群".to_string(), "草稿".to_string()));
    }

    #[test]
    fn rejects_empty_and_invalid_input() {
        assert!(parse_request_line("").is_err());
        assert!(parse_request_line("   \r\n").is_err());
        assert!(parse_request_line("not json").is_err());
        assert!(parse_request_line("[]").is_err());
        assert!(parse_request_line(r#"{"chatTitle":"群"}"#).is_err());
        assert!(parse_request_line(r#"{"text":"草稿"}"#).is_err());

        let wrong_type = r#"{"chatTitle":"群","text":42}"#;
        assert!(parse_request_line(wrong_type).is_err());
    }

    #[test]
    fn serializes_the_stdout_contract() {
        let success = FillResult {
            filled: true,
            chat_title: "目标聊天".to_string(),
            characters: 4,
        };
        let success_line = serde_json::to_string(&success).expect("should serialize");
        assert_eq!(success_line, r#"{"filled":true,"chatTitle":"目标聊天","characters":4}"#);

        let failure = FillFailure::new("输入框已有内容。");
        let failure_line = serde_json::to_string(&failure).expect("should serialize");
        assert_eq!(failure_line, r#"{"filled":false,"error":"输入框已有内容。"}"#);
    }
}

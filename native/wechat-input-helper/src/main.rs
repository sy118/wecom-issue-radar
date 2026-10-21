//! stdin/stdout driver for the personal WeChat fill helper.
//!
//! Reads exactly one JSON request line from stdin, performs the validated
//! UI Automation fill, and writes exactly one JSON response line to stdout.
//! No command-line arguments, files, registry keys, or network access are used.

use std::io::{self, BufRead, Read, Write};

use wechat_input_helper::{
    fill_wechat_input, parse_request_line, FillFailure, FillResult, MAX_INPUT_BYTES,
};

const EXIT_OK: i32 = 0;
const EXIT_FAILED: i32 = 1;
const INTERNAL_ERROR: &str = "填入过程中发生内部错误，已中止且未发送任何消息；请使用复制。";

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    match std::panic::catch_unwind(execute) {
        Ok(Ok(result)) => write_success(&result),
        Ok(Err(error)) => {
            let _ = write_response(&FillFailure::new(error));
            EXIT_FAILED
        }
        Err(_) => {
            let _ = write_response(&FillFailure::new(INTERNAL_ERROR));
            EXIT_FAILED
        }
    }
}

fn write_success(result: &FillResult) -> i32 {
    if write_response(result).is_ok() {
        EXIT_OK
    } else {
        EXIT_FAILED
    }
}

fn execute() -> Result<FillResult, String> {
    let line = read_input_line()?;
    let (chat_title, text) = parse_request_line(&line)?;
    fill_wechat_input(&chat_title, &text)
}

/// Reads one line from stdin, bounded by [`MAX_INPUT_BYTES`].
///
/// The caller may keep the pipe open after writing the line; reading stops at
/// the first newline or at EOF, whichever comes first. Only the first line is
/// used and any remaining input is ignored.
fn read_input_line() -> Result<String, String> {
    let stdin = io::stdin();
    let mut reader = stdin.lock().take(MAX_INPUT_BYTES as u64 + 1);
    let mut raw = Vec::new();
    reader
        .read_until(b'\n', &mut raw)
        .map_err(|error| format!("读取标准输入失败：{error}"))?;
    if raw.len() > MAX_INPUT_BYTES {
        let message = format!("输入过大：单行 JSON 不得超过 {MAX_INPUT_BYTES} 字节。");
        return Err(message);
    }
    if raw.is_empty() {
        return Err("标准输入为空，请写入一行 JSON。".to_string());
    }
    String::from_utf8(raw).map_err(|_| "标准输入不是有效的 UTF-8 文本。".to_string())
}

fn write_response<T: serde::Serialize>(response: &T) -> io::Result<()> {
    let line = serde_json::to_string(response).map_err(io::Error::other)?;
    let mut stdout = io::stdout().lock();
    stdout.write_all(line.as_bytes())?;
    stdout.write_all(b"\n")?;
    stdout.flush()
}

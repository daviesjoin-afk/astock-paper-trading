//! Blocking HTTP transport for the operational CLI commands.

use std::time::Duration;

use serde_json::Value;

pub(crate) fn agent(timeout: Duration, https_only: bool) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .https_only(https_only)
        .user_agent("Mozilla/5.0 (compatible; astock-v2)")
        .build()
        .into()
}

pub(crate) fn json(url: &str, payload: Option<&Value>, timeout: Duration) -> Result<Value, String> {
    let agent = agent(timeout, false);
    let mut response = match payload {
        Some(payload) => agent
            .post(url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .send(payload.to_string().as_bytes()),
        None => agent.get(url).header("Accept", "application/json").call(),
    }
    .map_err(|error| format!("HTTP request failed: {error}"))?;
    let body = response
        .body_mut()
        .read_to_vec()
        .map_err(|error| format!("cannot read HTTP response: {error}"))?;
    serde_json::from_slice(&body).map_err(|error| format!("invalid JSON response: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;

    #[test]
    fn json_selects_get_or_post_and_reports_invalid_json() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let timeout = Duration::from_secs(3);
        let server = std::thread::spawn(move || {
            for (request_line, expected_body, reply) in [
                ("GET /status HTTP/1.1", "", "{\"ready\":true}"),
                ("POST /round HTTP/1.1", "{\"count\":1}", "invalid JSON"),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(timeout)).unwrap();
                let mut reader = BufReader::new(&mut stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                assert_eq!(line.trim_end(), request_line);
                let mut content_length = 0;
                loop {
                    line.clear();
                    assert!(reader.read_line(&mut line).unwrap() > 0);
                    if line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        content_length = value.trim().parse().unwrap();
                    }
                }
                let mut body = vec![0; content_length];
                reader.read_exact(&mut body).unwrap();
                assert_eq!(body, expected_body.as_bytes());
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{reply}",
                    reply.len()
                )
                .unwrap();
            }
        });
        assert_eq!(
            json(&format!("{base}/status"), None, timeout).unwrap(),
            serde_json::json!({"ready": true})
        );
        let error = json(
            &format!("{base}/round"),
            Some(&serde_json::json!({"count": 1})),
            timeout,
        )
        .unwrap_err();
        assert!(error.starts_with("invalid JSON response:"));
        server.join().unwrap();
    }
}

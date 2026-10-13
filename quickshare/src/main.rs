//! JSON-lines bridge to RQuickShare's protocol engine. No window or tray.
mod discovery;
use rqs_lib::{
    channel::{ChannelDirection, ChannelMessage},
    EndpointInfo, OutboundPayload, SendInfo, Visibility, RQS,
};
use serde_json::{json, Value};
use std::{
    io::{self, Write},
    path::PathBuf,
};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    sync::broadcast,
};

struct ProtocolLog;
impl log::Log for ProtocolLog {
    fn enabled(&self, _: &log::Metadata) -> bool {
        true
    }
    fn log(&self, record: &log::Record) {
        // Never record payloads, identities, filenames, PINs or URLs.
        // Upstream logs include those fields, so retain only known phases and
        // source locations of errors, rather than forwarding its messages.
        if record.level() > log::Level::Warn
            && !(record.file().unwrap_or("").ends_with("inbound.rs")
                && matches!(record.line(), Some(824 | 888 | 945 | 720)))
        {
            return;
        }
        let message = record.args().to_string();
        if matches!(
            message.as_str(),
            "process_introduction: handling file_metadata"
                | "process_introduction: handling text_metadata"
                | "process_introduction: handling wifi_credentials_metadata"
                | "Transfer canceled"
        ) {
            emit(json!({"event":"diagnostic","phase":message}));
        } else if record.level() <= log::Level::Warn {
            emit(
                json!({"event":"diagnostic","phase":"engine-error","file":record.file(),"line":record.line()}),
            );
        }
    }
    fn flush(&self) {}
}
static PROTOCOL_LOG: ProtocolLog = ProtocolLog;

fn emit(event: Value) {
    let mut out = io::stdout().lock();
    let _ = writeln!(out, "{event}");
    let _ = out.flush();
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    log::set_logger(&PROTOCOL_LOG).map_err(|_| "Unable to initialize protocol diagnostics")?;
    log::set_max_level(log::LevelFilter::Off);
    let directory = std::env::args().nth(1).map(PathBuf::from);
    let port = std::env::args()
        .nth(2)
        .map(|v| v.parse::<u32>())
        .transpose()?;
    let mut rqs = RQS::new(Visibility::Visible, port, directory);
    let mut messages = rqs.message_sender.subscribe();
    let (sender, _) = rqs.run().await?;
    let (discovery, mut peers) = broadcast::channel::<EndpointInfo>(100);
    let discovery_task = discovery::start(discovery)?;
    emit(json!({"event":"ready"}));
    log::set_max_level(log::LevelFilter::Trace);
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => break,
            peer = peers.recv() => match peer {
                Ok(peer) => emit(json!({"event":"peer","data":peer})),
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(_) => break,
            },
            message = messages.recv() => match message {
                Ok(message) if message.direction == ChannelDirection::LibToFront => emit(json!({"event":"transfer","data":message})),
                Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(_) => break,
            },
            line = lines.next_line() => {
                let Some(line) = line? else { break };
                let result: Result<(), Box<dyn std::error::Error>> = async {
                    let cmd: Value = serde_json::from_str(&line)?;
                    match cmd["action"].as_str().unwrap_or("") {
                        "directory" => rqs.set_download_path(cmd["path"].as_str().map(PathBuf::from)),
                        "send" => {
                            let files = cmd["files"].as_array().ok_or("Missing files")?.iter()
                                .map(|x| x.as_str().map(str::to_owned).ok_or("Invalid file path"))
                                .collect::<Result<Vec<_>, _>>()?;
                            sender.send(SendInfo {
                                id:cmd["id"].as_str().ok_or("Missing id")?.into(),
                                name:cmd["name"].as_str().ok_or("Missing name")?.into(),
                                addr:cmd["addr"].as_str().ok_or("Missing addr")?.into(),
                                ob:OutboundPayload::Files(files),
                            }).await?;
                        },
                        "AcceptTransfer" | "RejectTransfer" | "CancelTransfer" => {
                            let message: ChannelMessage = serde_json::from_value(json!({
                                "id":cmd["id"], "direction":"FrontToLib", "action":cmd["action"],
                                "rtype":null, "state":null, "meta":null
                            }))?;
                            rqs.message_sender.send(message)?;
                        },
                        _ => return Err("Unknown command".into()),
                    }
                    Ok(())
                }.await;
                if let Err(error) = result { emit(json!({"event":"error","error":error.to_string()})); }
            }
        }
    }
    discovery_task.abort();
    rqs.stop().await;
    Ok(())
}

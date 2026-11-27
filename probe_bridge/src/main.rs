use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use anyhow::{Context, Result, ensure};
use probe_rs::flashing::{self, DownloadOptions, Format};
use probe_rs::probe::{DebugProbeInfo, WireProtocol, list::Lister};
use probe_rs::{MemoryInterface, Permissions, Session};
use serde::Deserialize;
use serde_json::{Value, json};

const MAX_WORDS: usize = 96 * 5;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    List,
    Open { index: usize, chip: String, speed: u32 },
    Close,
    Read { address: u64, count: usize },
    Write { address: u64, words: Vec<u32> },
    // 按地址和长度选择 32/16/8 位访问，每个元素一次写入，避免改到相邻变量。
    WriteBytes { address: u64, bytes: Vec<u8> },
    Flash { file: PathBuf },
    Quit,
}

#[derive(Default)]
struct Bridge {
    probes: Vec<DebugProbeInfo>,
    session: Option<Session>,
}

impl Bridge {
    fn session(&mut self) -> Result<&mut Session> {
        self.session.as_mut().context("SWD session is closed")
    }

    fn handle(&mut self, request: Request) -> Result<Value> {
        match request {
            Request::List => {
                self.probes = Lister::new().list_all();
                Ok(json!(self.probes.iter().enumerate().map(|(index, probe)| {
                    let label = if let Some(serial) = &probe.serial_number {
                        format!("{} · {}", probe.identifier.trim(), serial)
                    } else {
                        probe.identifier.trim().to_owned()
                    };
                    (label, index)
                }).collect::<Vec<_>>()))
            }
            Request::Open { index, chip, speed } => {
                self.session = None;
                let item = self.probes.get(index).context("Probe selection expired")?;
                let mut probe = item.open().with_context(|| format!("Open {}", item.identifier))?;
                probe.select_protocol(WireProtocol::Swd).context("Select SWD")?;
                let effective_speed = probe.set_speed(speed).context("Set SWD speed")?;
                self.session = Some(probe.attach(chip.as_str(), Permissions::default())
                    .with_context(|| format!("Attach to {chip} over SWD"))?);
                Ok(json!({"speed": effective_speed}))
            }
            Request::Close | Request::Quit => {
                self.session = None;
                Ok(Value::Null)
            }
            Request::Read { address, count } => {
                ensure!(count <= MAX_WORDS && address % 4 == 0, "Invalid word read");
                let mut words = vec![0u32; count];
                self.session()?.core(0)?.read_32(address, &mut words)?;
                Ok(json!(words))
            }
            Request::Write { address, words } => {
                ensure!(words.len() <= MAX_WORDS && address % 4 == 0, "Invalid word write");
                let mut core = self.session()?.core(0)?;
                core.write_32(address, &words)?;
                core.flush()?;
                Ok(Value::Null)
            }
            Request::WriteBytes { address, bytes } => {
                ensure!(!bytes.is_empty() && bytes.len() <= MAX_WORDS * 4, "Invalid byte write");
                let mut core = self.session()?.core(0)?;
                if address % 4 == 0 && bytes.len() % 4 == 0 {
                    let words: Vec<u32> = bytes.chunks(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
                    core.write_32(address, &words)?;
                } else if address % 2 == 0 && bytes.len() % 2 == 0 {
                    let halves: Vec<u16> = bytes.chunks(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
                    core.write_16(address, &halves)?;
                } else {
                    core.write_8(address, &bytes)?;
                }
                core.flush()?;
                Ok(Value::Null)
            }
            Request::Flash { file } => {
                let suffix = file.extension().and_then(|s| s.to_str()).unwrap_or_default().to_ascii_lowercase();
                let format = match suffix.as_str() {
                    "hex" => Format::Hex,
                    "elf" | "axf" => Format::Elf(Default::default()),
                    _ => anyhow::bail!("Unsupported flash image: {suffix}"),
                };
                let session = self.session()?;
                let mut options = DownloadOptions::default();
                options.verify = true;
                options.keep_unwritten_bytes = true;
                flashing::download_file_with_options(session, &file, format, options)
                    .context("Flash write/verify failed")?;
                session.core(0)?.reset().context("Image verified but reset failed")?;
                Ok(Value::Null)
            }
        }
    }
}

fn main() -> io::Result<()> {
    let input = io::stdin().lock();
    let mut output = io::BufWriter::new(io::stdout().lock());
    let mut bridge = Bridge::default();
    for line in input.lines() {
        let line = line?;
        let parsed = serde_json::from_str::<Request>(&line);
        let quitting = matches!(&parsed, Ok(Request::Quit));
        let result = parsed.map_err(anyhow::Error::from).and_then(|request| bridge.handle(request));
        let response = match result {
            Ok(data) => json!({ "ok": true, "data": data }),
            Err(error) => json!({ "ok": false, "error": format!("{error:#}") }),
        };
        serde_json::to_writer(&mut output, &response)?;
        output.write_all(b"\n")?;
        output.flush()?;
        if quitting {
            break;
        }
    }
    Ok(())
}

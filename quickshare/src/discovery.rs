//! Resolve advertised recipients with bounded checks of alternate addresses.
//! Upstream probes one arbitrary IPv4 address without a timeout, blocking all
//! subsequent discovery when a VPN/stale address cannot be reached.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use get_if_addrs::{get_if_addrs, IfAddr};
use mdns_sd::{ServiceDaemon, ServiceEvent};
use rqs_lib::{DeviceType, EndpointInfo};
use std::{collections::HashMap, net::Ipv4Addr};
use tokio::{
    net::TcpStream,
    sync::broadcast,
    task::JoinHandle,
    time::{timeout, Duration},
};

async fn reachable_address(addresses: &[Ipv4Addr], port: u16) -> Option<Ipv4Addr> {
    for ip in addresses.iter().take(4) {
        if let Ok(Ok(_stream)) =
            timeout(Duration::from_millis(750), TcpStream::connect((*ip, port))).await
        {
            return Some(*ip);
        }
    }
    None
}

fn decode_name(value: &str) -> Option<(DeviceType, String)> {
    let bytes = URL_SAFE_NO_PAD.decode(value).ok()?;
    if bytes.len() < 17 {
        return None;
    }
    let device_type = DeviceType::from_raw_value((bytes[0] >> 1) & 7);
    // Android can omit the optional name while still advertising a valid
    // endpoint (17-byte header). Do not silently drop these recipients.
    if bytes.len() == 17 {
        let name = match device_type {
            DeviceType::Phone => "Android phone",
            DeviceType::Tablet => "Nearby tablet",
            DeviceType::Laptop => "Nearby computer",
            DeviceType::Unknown => "Nearby device",
        };
        return Some((device_type, name.to_owned()));
    }
    let end = 18 + bytes[17] as usize;
    let name = std::str::from_utf8(bytes.get(18..end)?).ok()?.to_owned();
    if name.trim().is_empty() {
        return None;
    }
    Some((device_type, name))
}

pub fn start(
    sender: broadcast::Sender<EndpointInfo>,
) -> Result<JoinHandle<()>, Box<dyn std::error::Error>> {
    let daemon = ServiceDaemon::new()?;
    let receiver = daemon.browse("_FC9F5ED42C8A._tcp.local.")?;
    Ok(tokio::spawn(async move {
        let mut cache = HashMap::<String, String>::new();
        while let Ok(event) = receiver.recv_async().await {
            match event {
                ServiceEvent::ServiceResolved(info) => {
                    let Some((rtype, name)) = info
                        .get_property("n")
                        .and_then(|n| decode_name(n.val_str()))
                    else {
                        continue;
                    };
                    let local: Vec<_> = get_if_addrs()
                        .unwrap_or_default()
                        .into_iter()
                        .filter_map(|interface| match interface.addr {
                            IfAddr::V4(addr) => Some(addr),
                            _ => None,
                        })
                        .collect();
                    let mut addresses: Vec<Ipv4Addr> = info
                        .get_addresses_v4()
                        .into_iter()
                        .copied()
                        .filter(|ip| {
                            !ip.is_loopback()
                                && !ip.is_unspecified()
                                && !ip.is_multicast()
                                && !local.iter().any(|addr| addr.ip == *ip)
                        })
                        .collect();
                    // Prefer an address on a directly attached subnet. VPN addresses
                    // do not win merely because HashSet iteration chose them first.
                    addresses.sort_by_key(|ip| {
                        (
                            !local.iter().any(|addr| {
                                (u32::from(addr.ip) & u32::from(addr.netmask))
                                    == (u32::from(*ip) & u32::from(addr.netmask))
                            }),
                            !ip.is_private(),
                            u32::from(*ip),
                        )
                    });
                    let fullname = info.get_fullname().to_owned();
                    let port = info.get_port();
                    let Some(ip) = reachable_address(&addresses, port).await else {
                        if let Some(id) = cache.remove(&fullname) {
                            let _ = sender.send(EndpointInfo {
                                id,
                                present: Some(false),
                                ..Default::default()
                            });
                        }
                        continue;
                    };
                    let id = format!("{ip}:{port}");
                    if let Some(old) = cache.insert(fullname.clone(), id.clone()) {
                        if old != id {
                            let _ = sender.send(EndpointInfo {
                                id: old,
                                present: Some(false),
                                ..Default::default()
                            });
                        }
                    }
                    let _ = sender.send(EndpointInfo {
                        fullname,
                        id,
                        name: Some(name),
                        ip: Some(ip.to_string()),
                        port: Some(port.to_string()),
                        rtype: Some(rtype),
                        present: Some(true),
                    });
                }
                ServiceEvent::ServiceRemoved(_, fullname) => {
                    if let Some(id) = cache.remove(&fullname) {
                        let _ = sender.send(EndpointInfo {
                            id,
                            present: Some(false),
                            ..Default::default()
                        });
                    }
                }
                _ => {}
            }
        }
        let _ = daemon.shutdown();
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn tries_an_alternate_address_after_a_dead_endpoint() {
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let addresses = [Ipv4Addr::new(127, 0, 0, 2), Ipv4Addr::LOCALHOST];
        assert_eq!(
            reachable_address(&addresses, port).await,
            Some(Ipv4Addr::LOCALHOST)
        );
    }
    #[test]
    fn validates_endpoint_name_lengths_and_utf8() {
        let mut bytes = vec![2; 18];
        bytes[17] = 4;
        bytes.extend(b"Test");
        assert_eq!(
            decode_name(&URL_SAFE_NO_PAD.encode(&bytes)).unwrap().1,
            "Test"
        );
        bytes[17] = 5;
        assert!(decode_name(&URL_SAFE_NO_PAD.encode(&bytes)).is_none());
        assert!(decode_name("invalid!").is_none());
        assert_eq!(
            decode_name("MiXUQ5RfaHlyvBAC1xBqXdo").unwrap().1,
            "Android phone"
        );
    }
}

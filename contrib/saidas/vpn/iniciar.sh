#!/bin/sh
# Sobe a VPN, espera o tun0, sobe o SOCKS e encerra o container se qualquer um dos dois cair
# (o restart do compose sobe tudo de novo).
set -eu
cd /vpn
openvpn --config "/vpn/${OVPN:?defina OVPN}" --cd /vpn --verb 3 --daemon --log /tmp/openvpn.log
for i in $(seq 1 60); do
  ip addr show tun0 >/dev/null 2>&1 && break
  sleep 1
done
if ! ip addr show tun0 >/dev/null 2>&1; then
  echo "tun0 não subiu em 60 s:" >&2; tail -20 /tmp/openvpn.log >&2; exit 1
fi
echo "VPN ativa: $(ip -4 -o addr show tun0 | awk '{print $4}')"
sockd -f /etc/sockd.conf &
while kill -0 $! 2>/dev/null && pidof openvpn >/dev/null && ip addr show tun0 >/dev/null 2>&1; do sleep 10; done
echo "VPN ou SOCKS caiu; encerrando" >&2
tail -20 /tmp/openvpn.log >&2
exit 1

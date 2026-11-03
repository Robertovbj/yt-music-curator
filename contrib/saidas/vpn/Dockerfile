# Saída de rede por OpenVPN para o yt-dlp: o túnel vale só dentro deste container, e o dante expõe um
# SOCKS5 que sai SOMENTE pelo tun0 (se a VPN cair, o proxy falha em vez de vazar pela conexão de casa).
FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f
RUN apk add --no-cache openvpn dante-server
COPY sockd.conf /etc/sockd.conf
COPY iniciar.sh /usr/local/bin/iniciar.sh
RUN chmod +x /usr/local/bin/iniciar.sh
EXPOSE 1080
ENTRYPOINT ["/usr/local/bin/iniciar.sh"]

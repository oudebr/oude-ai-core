"""
HTTP seguro para URLs informadas por lojistas (ex.: API do fornecedor).
Evita SSRF: só https, e recusa hosts que resolvem para rede interna/loopback/metadata da nuvem.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class UrlNaoPermitida(Exception):
    pass


def _ip_proibido(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


async def validar_url_externa(url: str) -> str:
    partes = urlparse(url)
    if partes.scheme != "https" or not partes.hostname:
        raise UrlNaoPermitida("Somente URLs https são aceitas.")
    if partes.username or partes.password:
        raise UrlNaoPermitida("URL não pode conter usuário/senha.")
    host = partes.hostname
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, partes.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UrlNaoPermitida("Domínio não encontrado.") from exc
        ips = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not ips or any(_ip_proibido(ip) for ip in ips):
        raise UrlNaoPermitida("Endereço de rede interna não é permitido.")
    return url

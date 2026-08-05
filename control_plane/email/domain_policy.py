from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import tldextract

from control_plane.email.local_part import normalize_local_part

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
_RESERVED_SUFFIXES = (".example", ".invalid", ".localhost", ".test")


@dataclass(frozen=True)
class DomainGuidance:
    domain: str
    registrable_domain: str
    recommended_domain: str
    apex_mx_risk: bool


def canonicalize_domain(value: str, *, allow_test: bool = True) -> str:
    raw = value.strip().casefold().rstrip(".")
    if not raw or len(raw) > 253:
        raise ValueError("domain length is invalid")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not email domains")
    try:
        canonical = raw.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("domain is not valid IDNA") from error
    labels = canonical.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise ValueError("domain labels are invalid")
    if canonical == "abrolia.com" or canonical.endswith(".abrolia.com"):
        raise ValueError("Abrolia-controlled domains cannot be claimed")
    if canonical == "localhost" or canonical.endswith(".localhost"):
        raise ValueError("localhost domains are not supported")
    if canonical.endswith(_RESERVED_SUFFIXES):
        if allow_test and canonical.endswith(".test"):
            return canonical
        raise ValueError("reserved domains are not supported")
    extracted = _EXTRACT(canonical)
    if not extracted.suffix or not extracted.domain:
        raise ValueError("public suffixes cannot be claimed")
    return canonical


def domain_guidance(value: str, *, allow_test: bool = True) -> DomainGuidance:
    domain = canonicalize_domain(value, allow_test=allow_test)
    if domain.endswith(".test"):
        registrable = domain
        apex = False
    else:
        extracted = _EXTRACT(domain)
        registrable = f"{extracted.domain}.{extracted.suffix}"
        apex = domain == registrable
    return DomainGuidance(
        domain=domain,
        registrable_domain=registrable,
        recommended_domain=(f"assistant.{domain}" if apex else domain),
        apex_mx_risk=apex,
    )


def canonicalize_mailbox(domain: str, local_part: str) -> tuple[str, str]:
    return canonicalize_domain(domain), normalize_local_part(local_part)

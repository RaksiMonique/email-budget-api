"""Regression tests for sender resolution: registrable-domain reduction across
multi-part TLDs, and forward-unwrapping that skips consumer forwarders (scanning
text then HTML) to reach the real bank."""
from app.extraction import sender_resolver as S
from app.extraction.models import ParsedEmail, SenderSource


def _parsed(*, from_header="", dkim=None, text="", html=""):
    return ParsedEmail(
        subject="",
        from_header=from_header,
        dkim_signatures=list(dkim or []),
        headers={},
        text_body=text,
        html_body=html,
    )


def test_registered_domain_simple():
    assert S.registered_domain("email.chase.com") == "chase.com"
    assert S.registered_domain("chase.com") == "chase.com"


def test_registered_domain_jamaica_multi_tld():
    # bank.com.jm must NOT collapse to the public suffix "com.jm"
    assert S.registered_domain("alerts.bank.com.jm") == "bank.com.jm"
    assert S.registered_domain("bank.co.jm") == "bank.co.jm"


def test_registered_domain_couk_multi_tld():
    assert S.registered_domain("mail.barclays.co.uk") == "barclays.co.uk"


def test_manual_forward_recovers_bank_from_body_text():
    """Manual forward: outer From/DKIM is the forwarder's gmail; the real bank
    (jncb.com) survives only in the quoted body 'From:'."""
    parsed = _parsed(
        from_header="Jane Doe <jane@gmail.com>",
        dkim=["v=1; d=gmail.com; s=20230601"],
        text="Sent from my iPhone\n\nBegin forwarded message:\nFrom: NCB Alerts <alerts@jncb.com>\nDate: ...\n\nAmount JMD 2,550.00",
    )
    r = S.resolve(parsed)
    assert r.domain == "jncb.com"
    assert r.source == SenderSource.BODY


def test_double_forward_skips_intermediate_consumer_account():
    """Forward-of-a-forward: an intermediate personal gmail 'From:' appears
    BEFORE the bank's 'From:'. The scanner must skip the consumer domain."""
    parsed = _parsed(
        from_header="Me <me@outlook.com>",
        dkim=["v=1; d=outlook.com"],
        text=(
            "FYI\n\nBegin forwarded message:\n"
            "From: A Friend <friend@gmail.com>\n\n"
            "Begin forwarded message:\n"
            "*From:* First Global <no-reply@gkco.com>\n\n"
            "Amount: 670.00"
        ),
    )
    r = S.resolve(parsed)
    assert r.domain == "gkco.com"


def test_html_only_forward_recovers_bank_from_html_body():
    """A forward with a stub/empty text part but the quoted 'From:' living in the
    HTML part — the resolver must fall through to scanning the HTML."""
    parsed = _parsed(
        from_header="Me <me@icloud.com>",
        dkim=["v=1; d=icloud.com"],
        text="",
        html=(
            "<div>Sent from my iPhone</div>"
            "<blockquote>From: VMBS &lt;alerts@myvmgroup.com&gt;<br>"
            "An amount of $9,500.00 was authorized</blockquote>"
        ),
    )
    r = S.resolve(parsed)
    assert r.domain == "myvmgroup.com"
    assert r.source == SenderSource.BODY


def test_direct_bank_dkim_wins_no_unwrapping():
    """A real (auto-forwarded) bank alert whose DKIM d= survives is trusted
    directly — no body unwrapping needed."""
    parsed = _parsed(
        from_header="Chase <no.reply.alerts@chase.com>",
        dkim=["v=1; a=rsa-sha256; d=chase.com; s=prod"],
        text="Your card was used.",
    )
    r = S.resolve(parsed)
    assert r.domain == "chase.com"
    assert r.source == SenderSource.DKIM

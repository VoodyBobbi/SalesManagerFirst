# GigaChat TLS certificate

Russian Trusted Root CA, distributed only as an additional trust anchor for
GigaChat OAuth and API connections. Native Windows trust is tried first; this
bundled CA is loaded automatically only after certificate verification fails.
The application keeps certificate and hostname verification enabled and does not
modify the operating system certificate store. No user certificate setting exists.

Official integration guidance:
https://developers.sber.ru/docs/ru/gigachat/certificates

Official certificate source:
https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt

Retrieved through verified HTTPS on 2026-11-12.
Certificate validity: 2022-03-01 21:04:15 UTC through 2032-02-27 21:04:15 UTC.
SHA-256 fingerprint of the DER certificate:
`d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31`

When replacing the certificate, verify the official source, fingerprint, subject,
validity and CA constraints, run the TLS tests, then rebuild the executable.

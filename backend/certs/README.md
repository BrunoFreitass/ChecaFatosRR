# Certificado do banco (Supabase)

Salve aqui o certificado baixado em *Supabase → Project Settings → Database → SSL Configuration*
com o nome **`supabase-ca.crt`**.

- É o certificado **público** da autoridade (CA) do Supabase: não é segredo e pode ir para o git.
- Ele entra na imagem pelo `Dockerfile` (`COPY certs ./certs`) e o Cloud Run o encontra em
  `/srv/certs/supabase-ca.crt` (variável `DATABASE_SSL_ROOT_CERT`, definida no deploy).
- Local: no `backend/.env`, use `DATABASE_SSL_ROOT_CERT=certs/supabase-ca.crt`.

Com ele, a conexão valida a cadeia e o nome do servidor (verify-full). Sem ele, a conexão é
criptografada, mas o certificado não é validado.

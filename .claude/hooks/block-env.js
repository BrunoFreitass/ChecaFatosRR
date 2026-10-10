#!/usr/bin/env node
// PreToolUse: bloqueia qualquer acesso a arquivos .env* reais (só *.env.example é liberado).
// Cobre Read/Edit/Write/Grep/Glob (caminhos e globs) e Bash/PowerShell (texto do comando).
// Saída 2 = chamada bloqueada; a mensagem no stderr volta para o agente.

// Falha fechada: entrada ilegível ou erro inesperado bloqueia a chamada em vez de liberá-la.
const deny = (reason) => {
  process.stderr.write(`block-env: ${reason}; chamada bloqueada por segurança.\n`);
  process.exit(2);
};
process.on("uncaughtException", (err) => deny(`erro interno (${err && err.message})`));

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("error", () => deny("stdin ilegível"));
process.stdin.on("end", () => {
  let input;
  try {
    input = JSON.parse(raw);
  } catch {
    deny("entrada do hook não é JSON válido");
  }
  if (!input || typeof input !== "object") deny("entrada do hook vazia");
  const tool = input.tool_name || "";
  const args = input.tool_input || {};

  // Nome de arquivo .env, .env.local, .env.production... mas não .env.example.
  const isSecretName = (name) => /^\.env(\.[^\\/]*)?$/i.test(name) && !/\.example$/i.test(name);
  const basename = (p) => String(p).split(/[\\/]/).pop();

  // Ocorrência de .env* como token dentro de um comando ou glob.
  const TOKEN = /(^|[\s\\/'"=:`(,])(\.env(?:\.[\w.*-]+)?|\.env\*)(?=$|[\s'"`|;&)<>,])/gi;
  const mentionsSecret = (text) => {
    for (const m of String(text).matchAll(TOKEN)) {
      if (!/\.example$/i.test(m[2])) return true;
    }
    return false;
  };

  let blocked = false;
  if (tool === "Bash" || tool === "PowerShell") {
    blocked = mentionsSecret(args.command || "");
  } else {
    for (const key of ["file_path", "path", "notebook_path"]) {
      if (args[key] && isSecretName(basename(args[key]))) blocked = true;
    }
    for (const key of ["glob", "pattern"]) {
      if (tool !== "Grep" || key === "glob") {
        if (args[key] && mentionsSecret(args[key])) blocked = true;
      }
    }
  }

  if (blocked) {
    process.stderr.write(
      "Bloqueado pela regra do projeto: arquivos .env reais (backend/.env, frontend/.env.local, " +
        "**/.env*) nunca podem ser lidos, editados ou citados em comandos. Use apenas *.env.example.\n",
    );
    process.exit(2);
  }
  process.exit(0);
});

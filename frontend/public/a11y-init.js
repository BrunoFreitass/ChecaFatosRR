// Menu de acessibilidade: aplica as preferências salvas ANTES da 1ª pintura (sem piscar).
// Arquivo nosso, servido pelo próprio site (a CSP permite script-src 'self'; nada inline).
// ESPELHO de src/lib/a11y.ts (o teste a11y.test.ts confere que os dois aplicam as mesmas classes).
(function () {
  try {
    var raw = JSON.parse(window.localStorage.getItem("checafatos:a11y") || "null");
    if (!raw || typeof raw !== "object") return;
    var root = document.documentElement;
    var fonts = { "-1": "a11y-fonte-menos", 1: "a11y-fonte-1", 2: "a11y-fonte-2", 3: "a11y-fonte-3" };
    if (typeof raw.fonte === "number" && fonts[raw.fonte]) root.classList.add(fonts[raw.fonte]);
    var toggles = ["contraste", "inverter", "cinza", "links", "espaco", "pausar", "cursor"];
    for (var i = 0; i < toggles.length; i++) {
      // Contraste e inverter se excluem (juntos, a página fica branca): vale o contraste.
      if (toggles[i] === "inverter" && raw.contraste === true) continue;
      if (raw[toggles[i]] === true) root.classList.add("a11y-" + toggles[i]);
    }
  } catch (e) {
    // Sem armazenamento ou valor estragado: página no padrão.
  }
})();

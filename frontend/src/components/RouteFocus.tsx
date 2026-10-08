import { useEffect, useRef } from "react";
import { useLocation } from "react-router";

/**
 * Numa SPA o leitor de tela não percebe a troca de página. A cada navegação (exceto a
 * primeira carga), move o foco para o conteúdo principal, que tem o título da página.
 */
export function RouteFocus({ targetId }: { targetId: string }) {
  const { pathname } = useLocation();
  const isFirstRender = useRef(true);

  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    document.getElementById(targetId)?.focus();
  }, [pathname, targetId]);

  return null;
}

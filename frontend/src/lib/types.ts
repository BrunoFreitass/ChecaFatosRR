export type ProfileStatus = "active" | "suspended" | "banned";

export interface Profile {
  apelido: string;
  status: ProfileStatus;
  suspended_until: string | null;
  is_admin: boolean;
  terms_version: string;
  created_at: string;
}

export type NicknameRejection = "formato" | "reservado" | "conteudo_proibido" | "em_uso";

export interface NicknameAvailability {
  disponivel: boolean;
  motivo: NicknameRejection | null;
}

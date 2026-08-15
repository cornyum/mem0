export interface Category {
  name: string;
  description: string;
}

export interface CategoryListResponse {
  categories: Category[];
  updated_at: string | null;
}

export interface Memory {
  id: string;
  memory: string;
  user_id?: string;
  agent_id?: string;
  tenant_id?: string | null;
  session_id?: string | null;
  metadata?: {
    categories?: string[];
    [key: string]: unknown;
  };
  created_at?: string;
  updated_at?: string;
}

export interface ApiKey {
  id: string;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiKeyCreateResponse {
  id: string;
  label: string;
  key: string;
  key_prefix: string;
  created_at: string;
}

export interface ApiRequestLog {
  id: string;
  created_at: string;
  method: string;
  path: string;
  status_code: number;
  latency_ms: number;
  auth_type: string;
}

export type EntityType = "user" | "agent" | "run" | "tenant" | "session";

export interface Entity {
  id: string;
  type: EntityType;
  total_memories: number;
  created_at: string | null;
  updated_at: string | null;
}

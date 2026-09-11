-- HUYỀN VŨ VĂN BẢN AI — PostgreSQL schema V2.0
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','suspended','locked','archived')),
  email_verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  slug text UNIQUE NOT NULL,
  org_type text NOT NULL DEFAULT 'agency',
  owner_user_id uuid NOT NULL REFERENCES users(id),
  status text NOT NULL DEFAULT 'active',
  data_policy text NOT NULL DEFAULT 'internal' CHECK (data_policy IN ('public','internal','confidential','restricted')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS departments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  parent_id uuid REFERENCES departments(id) ON DELETE SET NULL,
  name text NOT NULL,
  code text,
  type text NOT NULL DEFAULT 'department',
  status text NOT NULL DEFAULT 'active',
  UNIQUE(organization_id, code)
);

CREATE TABLE IF NOT EXISTS memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  department_id uuid REFERENCES departments(id) ON DELETE SET NULL,
  role text NOT NULL DEFAULT 'member',
  status text NOT NULL DEFAULT 'active',
  joined_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(organization_id, user_id, department_id)
);

CREATE TABLE IF NOT EXISTS plans (
  id text PRIMARY KEY,
  name text NOT NULL,
  monthly_price_vnd bigint NOT NULL DEFAULT 0,
  included_credits bigint NOT NULL DEFAULT 0,
  max_members integer,
  max_storage_mb bigint,
  allowed_model_tiers jsonb NOT NULL DEFAULT '["economy"]'::jsonb,
  features jsonb NOT NULL DEFAULT '{}'::jsonb,
  active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type text NOT NULL CHECK (scope_type IN ('user','organization')),
  scope_id uuid NOT NULL,
  plan_id text NOT NULL REFERENCES plans(id),
  status text NOT NULL DEFAULT 'active',
  current_period_start timestamptz NOT NULL DEFAULT now(),
  current_period_end timestamptz,
  cancel_at_period_end boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wallets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type text NOT NULL CHECK (scope_type IN ('user','organization','department')),
  scope_id uuid NOT NULL,
  balance_credits bigint NOT NULL DEFAULT 0,
  monthly_budget_credits bigint,
  hard_limit_credits bigint,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  wallet_id uuid NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
  delta_credits bigint NOT NULL,
  balance_after bigint NOT NULL,
  event_type text NOT NULL,
  reference_type text,
  reference_id text,
  note text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_models (
  id text PRIMARY KEY,
  provider text NOT NULL,
  model_name text NOT NULL,
  display_name text NOT NULL,
  tier text NOT NULL CHECK (tier IN ('economy','standard','advanced','private')),
  input_usd_per_million numeric(12,6) NOT NULL DEFAULT 0,
  cached_input_usd_per_million numeric(12,6) NOT NULL DEFAULT 0,
  output_usd_per_million numeric(12,6) NOT NULL DEFAULT 0,
  service_multiplier numeric(8,4) NOT NULL DEFAULT 1.30,
  enabled boolean NOT NULL DEFAULT true,
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
  allowed_plans jsonb NOT NULL DEFAULT '[]'::jsonb,
  fallback_model_id text REFERENCES ai_models(id),
  daily_budget_usd numeric(12,4),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_usage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES users(id),
  organization_id uuid REFERENCES organizations(id),
  department_id uuid REFERENCES departments(id),
  wallet_id uuid REFERENCES wallets(id),
  model_id text REFERENCES ai_models(id),
  task_type text NOT NULL,
  input_tokens bigint NOT NULL DEFAULT 0,
  cached_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  provider_cost_usd numeric(16,8) NOT NULL DEFAULT 0,
  charged_credits bigint NOT NULL DEFAULT 0,
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  department_id uuid REFERENCES departments(id) ON DELETE SET NULL,
  direction text NOT NULL CHECK (direction IN ('incoming','outgoing','internal')),
  document_type text NOT NULL,
  standard text NOT NULL DEFAULT 'government' CHECK (standard IN ('government','party','other')),
  register_number text,
  source_number text,
  symbol text,
  sender text,
  recipient text,
  subject text NOT NULL DEFAULT '',
  issued_date date,
  received_at timestamptz,
  deadline timestamptz,
  priority text NOT NULL DEFAULT 'normal',
  confidentiality text NOT NULL DEFAULT 'internal',
  status text NOT NULL DEFAULT 'draft',
  parent_document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
  owner_user_id uuid REFERENCES users(id),
  current_version integer NOT NULL DEFAULT 1,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version integer NOT NULL,
  content_text text NOT NULL DEFAULT '',
  file_url text,
  created_by uuid REFERENCES users(id),
  ai_model_id text REFERENCES ai_models(id),
  change_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id, version)
);

CREATE TABLE IF NOT EXISTS document_assignments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  department_id uuid REFERENCES departments(id),
  assignee_user_id uuid REFERENCES users(id),
  role text NOT NULL DEFAULT 'primary',
  status text NOT NULL DEFAULT 'assigned',
  due_at timestamptz,
  assigned_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id bigserial PRIMARY KEY,
  organization_id uuid REFERENCES organizations(id),
  user_id uuid REFERENCES users(id),
  action text NOT NULL,
  entity_type text NOT NULL,
  entity_id text,
  before_data jsonb,
  after_data jsonb,
  ip_address inet,
  user_agent text,
  ai_model_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_org_direction ON documents(organization_id, direction, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_deadline ON documents(organization_id, deadline) WHERE deadline IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_org_created ON ai_usage(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_org_created ON audit_logs(organization_id, created_at DESC);

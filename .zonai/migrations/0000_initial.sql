CREATE TABLE "channels" (
  "closed" INTEGER NOT NULL,
  "closed_reason" TEXT,
  "created_at" INTEGER NOT NULL,
  "created_by" TEXT NOT NULL,
  "id" TEXT PRIMARY KEY,
  "max_messages" INTEGER NOT NULL,
  "message_count" INTEGER NOT NULL,
  "name" TEXT NOT NULL,
  "topic" TEXT,
  "updated_at" INTEGER
);

CREATE TABLE "memberships" (
  "channel" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  "done" INTEGER NOT NULL,
  "id" TEXT PRIMARY KEY,
  "identity" TEXT NOT NULL,
  "seen_seq" INTEGER NOT NULL,
  "updated_at" INTEGER
);

CREATE TABLE "messages" (
  "channel" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  "from_identity" TEXT NOT NULL,
  "id" TEXT PRIMARY KEY,
  "seq" INTEGER NOT NULL,
  "text" TEXT NOT NULL,
  "updated_at" INTEGER
);
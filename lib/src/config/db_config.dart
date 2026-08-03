import 'dart:io';

import 'package:zonai_schema/zonai_schema.dart';

/// Config for a chat room that runs on one machine and knows nobody.
///
/// The secrets below are real rather than "change-me" placeholders so a fresh
/// clone works without setup, and they guard nothing meaningful: there are no
/// accounts, and the rules are open (see `lib/src/rules/`). They exist because
/// zonai requires them, not because anything here is protected by them.
///
/// If this ever listens on something other than loopback, these stop being
/// decorative and need rotating along with a real auth table.
AppConfig main() {
  return AppConfig(
    appName: 'llm_chat',
    passwordSecret: Platform.environment['LLM_CHAT_PASSWORD_SECRET'] ??
        'l7QW-2mFyKcRr0dXpvA6ZbN4uEhJt1sYgOaLiCnM8xTfVbUqPeIkGwDzHjRsXm3B',
    jwtSecret: Platform.environment['LLM_CHAT_JWT_SECRET'] ??
        'Rk9pQzXwEa1NvYuBtM4hLdCgS7oJfI2rTnZmXqVbKeUyPcHjAsGl30WDiF8O5Nzx',
    baseUrl: Platform.environment['LLM_CHAT_BASE_URL'] ?? 'http://127.0.0.1:7717',
    // Never used — nothing here sends mail. Placeholders on purpose.
    email: EmailConfig(
      host: 'smtp.example.com',
      port: 587,
      username: 'user@example.com',
      password: 'unused',
      from: EmailAddress(address: 'noreply@example.com', name: 'llm_chat'),
    ),
  );
}

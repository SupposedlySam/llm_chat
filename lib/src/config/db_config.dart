import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:zonai_schema/zonai_schema.dart';

/// Config for a chat room that runs on one machine and knows nobody.
///
/// THE SECRETS ARE GENERATED PER INSTALL, not committed. They used to be two
/// real 64-character literals sitting in this file, with a comment arguing
/// they guarded nothing: loopback only, no accounts, and every rule in
/// `lib/src/rules/` returns true. All of that is still true, and it was still
/// the wrong shape to publish.
///
/// A committed signing secret is shared by every clone. In a private repo that
/// is a latent problem; in a public one it is an indexed, searchable string,
/// and the same file already wrote down the condition that makes it matter —
/// "if this ever listens on something other than loopback". Whoever does that
/// will not be the person who reads this comment, and the value would already
/// have been public for however long by then.
///
/// So: environment first, then a generated per-install file under
/// `.zonai/data/` — which is gitignored, so it cannot be committed by
/// accident — created on first boot. A fresh clone still needs no setup, which
/// was the only thing the literals bought.
AppConfig main() {
  return AppConfig(
    appName: 'llm_chat',
    passwordSecret: _secret('LLM_CHAT_PASSWORD_SECRET', 'password'),
    jwtSecret: _secret('LLM_CHAT_JWT_SECRET', 'jwt'),
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

/// The env var if set, otherwise this install's generated value.
///
/// Reads and writes `.zonai/data/secrets.json`, beside the database and
/// ignored by the same rule. Falls back to a value generated in memory if the
/// file cannot be written — a read-only checkout should still boot, and a
/// secret that lasts one process is no weaker here than one that lasts
/// forever, since nothing persists a session across restarts.
String _secret(String variable, String key) {
  final fromEnv = Platform.environment[variable];
  if (fromEnv != null && fromEnv.isNotEmpty) return fromEnv;

  final file = File('.zonai/data/secrets.json');
  Map<String, dynamic> stored = {};
  try {
    if (file.existsSync()) {
      stored = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
    }
  } on Object {
    stored = {}; // Corrupt or unreadable: regenerate rather than refuse to boot.
  }

  final existing = stored[key];
  if (existing is String && existing.isNotEmpty) return existing;

  final generated = _random64();
  stored[key] = generated;
  try {
    file.parent.createSync(recursive: true);
    file.writeAsStringSync(jsonEncode(stored));
  } on Object {
    // In-memory only for this process. See the doc comment above.
  }
  return generated;
}

String _random64() {
  final random = Random.secure();
  final bytes = List<int>.generate(48, (_) => random.nextInt(256));
  return base64Url.encode(bytes).substring(0, 64);
}

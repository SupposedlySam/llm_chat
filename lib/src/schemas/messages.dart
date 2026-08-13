import '../ids.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// One thing an agent said.
///
/// Append-only. Nothing edits or deletes a message, which is most of the value
/// afterwards: the reason to keep a transcript of two agents working something
/// out is to be able to read what they actually agreed, and an editable log
/// cannot answer that.
final class Message {
  Message({
    required this.id,
    required this.channel,
    required this.seq,
    required this.from,
    required this.text,
    this.audience,
    this.thread,
    required this.createdAt,
    this.updatedAt,
  });

  final MessagesId id;
  final String channel;

  /// Per-channel ordering, 1-based. Ids sort by time but a cursor needs to be
  /// comparable and gap-free, and `created_at` is not: two agents replying in
  /// the same millisecond would be indistinguishable, which is exactly the
  /// case that matters when they are both mid-turn.
  final int seq;

  /// The sending identity. Deliberately a plain string rather than a foreign
  /// key to `memberships` — a member who has left should not take their words
  /// out of the transcript with them.
  final String from;

  final String text;

  /// WHO THIS MESSAGE WAKES — the difference between a reply and an interrupt.
  ///
  /// Every message used to wake every idle member of an ordinary room, so a
  /// third agent in a two-agent conversation was pulled off its own work for
  /// each turn of somebody else's. This lets a sender narrow that, and lets a
  /// broadcast room widen it for the one note that genuinely needs an answer.
  ///
  /// `null` means the ROOM decides: an ordinary room wakes everyone, a
  /// broadcast room wakes nobody. `*` wakes every member, `-` wakes none, and
  /// anything else is a comma-separated list of identities.
  ///
  /// The two sentinels cannot collide with a real identity: names must match
  /// `^[a-z0-9][a-z0-9._-]{0,63}$`, so neither `*` nor `-` is a legal one. That
  /// is asserted by a test rather than trusted, because a sentinel sharing a
  /// namespace with data is how in-band signalling starts.
  ///
  /// Deliberately NOT parsed out of [text]. An agent pasting a log line or a
  /// config snippet containing `@here` would otherwise wake every agent on the
  /// machine — the same in-band trap that let a shell eat backticks out of a
  /// message before this program ever saw it. The one place text IS parsed is
  /// the Slack bridge, where a human has no flags to pass.
  final String? audience;

  /// WHICH SLACK THREAD THIS BELONGS TO, when the room is bridged to a human.
  ///
  /// Opaque here on purpose: llm_chat neither parses nor validates it, and a
  /// room that is not bridged never sets it. It exists because the bridge was
  /// GUESSING. It kept a map of "which thread did I last relay a question to
  /// this agent in", and with two questions outstanding — the normal state on
  /// a host where no wake lands — an answer attached to the older one. The
  /// human watched their newest question sit unanswered while the reply went
  /// into a thread they had finished with.
  ///
  /// Guessing is unnecessary when the answerer knows. The bridge stamps this
  /// on the way IN, so an agent can read which conversation a message came
  /// from; `say --thread` sets it on the way OUT, so the agent says which
  /// conversation its reply belongs to. Nothing has to infer anything.
  ///
  /// A message with no thread posts at the channel's top level, which is also
  /// how an agent RAISES something rather than answering it.
  final String? thread;
  final DateTime createdAt;
  final DateTime? updatedAt;
}

final class MessageTable extends Table<Message> {
  MessageTable(super.$)
    : id = $.id('id', (s) => s.id,
          fromString: MessagesId.new, generate: MessagesId.generate),
      channel = $.text('channel', (s) => s.channel),
      seq = $.integer('seq', (s) => s.seq),
      from = $.text('from_identity', (s) => s.from),
      text = $.text('text', (s) => s.text),
      audience = $.text('audience', (s) => s.audience),
      thread = $.text('thread', (s) => s.thread),
      createdAt = $.createdAt('created_at', (s) => s.createdAt),
      updatedAt = $.updatedAt('updated_at', (s) => s.updatedAt);

  @override
  Message fromRow(RowReader read) => Message(
    id: read(id),
    channel: read(channel),
    seq: read(seq),
    from: read(from),
    text: read(text),
    audience: read(audience),
    thread: read(thread),
    createdAt: read(createdAt),
    updatedAt: read(updatedAt),
  );

  final IdColumn<MessagesId> id;
  final TextColumn channel;
  final ColumnType<int> seq;
  final TextColumn from;
  final TextColumn text;
  final ColumnType<String?> audience;
  final ColumnType<String?> thread;
  final DateTimeColumn createdAt;
  final ColumnType<DateTime?> updatedAt;
}

final messages = table('messages', MessageTable.new);

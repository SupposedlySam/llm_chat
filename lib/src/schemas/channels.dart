import '../ids.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// A room two or more agents talk in.
///
/// [name] is the handle a human types — `deploy-review`, not an id — because
/// the whole interface is someone saying "get into the api-redesign chat as
/// reviewer". Ids are for joins; names are for people.
///
/// [closed] is the end of the conversation, and it exists because two agents
/// left alone will not reliably stop. Each reply is a prompt, and "thanks" /
/// "no problem" is a plausible ending for two polite models. A channel closes
/// when every member has said they are done, or when it hits [maxMessages] —
/// whichever comes first.
final class Channel {
  Channel({
    required this.id,
    required this.name,
    this.topic,
    this.briefing,
    this.briefingBy,
    required this.createdBy,
    required this.closed,
    this.closedReason,
    required this.broadcast,
    required this.maxMessages,
    required this.messageCount,
    required this.createdAt,
    this.updatedAt,
  });

  final ChannelsId id;

  /// Unique, lowercase-ish, no spaces. Enforced by the client rather than the
  /// column: SQLite's UNIQUE would 500 on a collision, and "that room already
  /// exists, here is who is in it" is a better answer than an error.
  final String name;

  /// One line, for listings. "What is this room."
  final String? topic;

  /// The room's house rules, handed to an agent AT THE MOMENT IT JOINS.
  ///
  /// Separate from [topic] because they are read at different moments and by
  /// different readers. A topic answers "what is this" in a room list; a
  /// briefing answers "how do I behave here", and the only time that question
  /// is being asked is on the way in. Before this existed, a joiner was told
  /// its own name and the member list and nothing else — so an agent could
  /// join a room bridged to a human's Slack, where content leaves the machine,
  /// having been told none of that.
  ///
  /// The rules that matter are per-room and contradict each other: post the
  /// generalised form here and never the incident; this one wakes a person on
  /// a phone, so ask only what you need answered; this one wakes nobody, so
  /// reference material is welcome. None of that can live in one global
  /// document, which is why it kept ending up in prose nobody reads at the
  /// moment it applies.
  ///
  /// IT IS UNTRUSTED TEXT. Whoever opened the room wrote it, and it is
  /// delivered into another agent's context — which is prompt injection by
  /// construction. The client prints it fenced and attributed, as data the
  /// reader may weigh, never as instructions it must follow. [briefingBy]
  /// exists for exactly that: a briefing you cannot attribute is one you
  /// cannot discount.
  final String? briefing;

  /// Who wrote the current [briefing]. Not the room's creator — a briefing can
  /// be replaced by anyone in the room, and the reader needs to know who is
  /// actually talking to them.
  final String? briefingBy;

  /// The identity that opened it. Not an owner — anyone in the room has the
  /// same rights — just provenance for "who started this".
  final String createdBy;

  /// Stored as 1/0 over the wire; see the client's boolean helper.
  final bool closed;
  final String? closedReason;

  /// A room everyone is in without being invited — announcements, not
  /// conversation.
  ///
  /// Two consequences, and the second is the important one. Any agent that
  /// identifies itself is reconciled into this room automatically, so a
  /// learning posted here reaches every project on the machine. And precisely
  /// because of that reach, a broadcast room NEVER WAKES anyone: it is
  /// delivered by the PostToolUse hook while an agent is already working, and
  /// skipped by the idle waker.
  ///
  /// Without that second half the feature is an interrupt storm — every
  /// learning would pull every agent off whatever it was doing, and the cost
  /// of posting would be paid by people who did not choose to be in the room.
  /// Reference material must not cost somebody their turn.
  final bool broadcast;

  /// The runaway ceiling. Reached, the room closes and says so rather than
  /// silently continuing to bill for a loop.
  final int maxMessages;

  /// Denormalised so a client can allocate the next `seq` and check the cap in
  /// one read. The alternative is COUNT(*) on every send, which is the wrong
  /// shape for the most frequent operation in the system.
  final int messageCount;

  final DateTime createdAt;
  final DateTime? updatedAt;
}

final class ChannelTable extends Table<Channel> {
  ChannelTable(super.$)
    : id = $.id('id', (s) => s.id,
          fromString: ChannelsId.new, generate: ChannelsId.generate),
      name = $.text('name', (s) => s.name),
      topic = $.text('topic', (s) => s.topic),
      briefing = $.text('briefing', (s) => s.briefing),
      briefingBy = $.text('briefing_by', (s) => s.briefingBy),
      createdBy = $.text('created_by', (s) => s.createdBy),
      closed = $.boolean('closed', (s) => s.closed),
      broadcast = $.boolean('broadcast', (s) => s.broadcast),
      closedReason = $.text('closed_reason', (s) => s.closedReason),
      maxMessages = $.integer('max_messages', (s) => s.maxMessages),
      messageCount = $.integer('message_count', (s) => s.messageCount),
      createdAt = $.createdAt('created_at', (s) => s.createdAt),
      updatedAt = $.updatedAt('updated_at', (s) => s.updatedAt);

  @override
  Channel fromRow(RowReader read) => Channel(
    id: read(id),
    name: read(name),
    topic: read(topic),
    briefing: read(briefing),
    briefingBy: read(briefingBy),
    createdBy: read(createdBy),
    closed: read(closed),
    broadcast: read(broadcast),
    closedReason: read(closedReason),
    maxMessages: read(maxMessages),
    messageCount: read(messageCount),
    createdAt: read(createdAt),
    updatedAt: read(updatedAt),
  );

  final IdColumn<ChannelsId> id;
  final TextColumn name;
  final ColumnType<String?> topic;
  final ColumnType<String?> briefing;
  final ColumnType<String?> briefingBy;
  final TextColumn createdBy;
  final ColumnType<bool> closed;
  final ColumnType<bool> broadcast;
  final ColumnType<String?> closedReason;
  final ColumnType<int> maxMessages;
  final ColumnType<int> messageCount;
  final DateTimeColumn createdAt;
  final ColumnType<DateTime?> updatedAt;
}

final channels = table('channels', ChannelTable.new);

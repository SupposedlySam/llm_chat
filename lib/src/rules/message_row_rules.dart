import '../schemas/messages.dart';
import 'package:zonai_schema/zonai_schema.dart';

MessageRowRules main() => MessageRowRules();

/// See the table rules for why this is open. Loopback only.
final class MessageRowRules extends RowRules<MessageTable, Message> {
  MessageRowRules() : super(messages);

  @override
  Future<bool> canCreate(Jwt? jwt, Message row) async => true;

  @override
  Future<bool> canView(Jwt? jwt, Message row) async => true;

  @override
  Future<bool> canUpdate(Jwt? jwt, Message before, Message after) async => true;

  @override
  Future<bool> canDelete(Jwt? jwt, Message row) async => true;
}

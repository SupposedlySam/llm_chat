import '../schemas/channels.dart';
import 'package:zonai_schema/zonai_schema.dart';

ChannelRowRules main() => ChannelRowRules();

/// See the table rules for why this is open. Loopback only.
final class ChannelRowRules extends RowRules<ChannelTable, Channel> {
  ChannelRowRules() : super(channels);

  @override
  Future<bool> canCreate(Jwt? jwt, Channel row) async => true;

  @override
  Future<bool> canView(Jwt? jwt, Channel row) async => true;

  @override
  Future<bool> canUpdate(Jwt? jwt, Channel before, Channel after) async => true;

  @override
  Future<bool> canDelete(Jwt? jwt, Channel row) async => true;
}

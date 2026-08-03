import 'package:zonai_schema/zonai_schema.dart' as z;

sealed class Id implements z.Id {
  const Id(this.value);

  factory Id.fromJson(String json) {
    final parts = json.split('_');
    if (parts.length != 2) {
      throw ArgumentError('Invalid ID format: $json');
    }
    return switch (parts[1]) {
      ChannelsId._suffix => ChannelsId(json),
      MembershipsId._suffix => MembershipsId(json),
      MessagesId._suffix => MessagesId(json),
      _ => throw ArgumentError('Invalid ID format: $json'),
    };
  }

  final String value;

  @override
  String toString() => value;

  String toJson() => value;

  @override
  bool operator ==(Object other) => other is Id && other.value == value;

  @override
  int get hashCode => value.hashCode;
}

class ChannelsId extends Id {
  ChannelsId(String value)
      : assert(() {
          final parts = value.split('_');
          return parts.length == 2 && parts[1] == _suffix;
        }(), 'Expected an ID with suffix $_suffix, got $value'),
        super(value);

  factory ChannelsId.generate() => ChannelsId(z.Id.generate(_suffix));

  static const _suffix = 'ch';
}

class MembershipsId extends Id {
  MembershipsId(String value)
      : assert(() {
          final parts = value.split('_');
          return parts.length == 2 && parts[1] == _suffix;
        }(), 'Expected an ID with suffix $_suffix, got $value'),
        super(value);

  factory MembershipsId.generate() => MembershipsId(z.Id.generate(_suffix));

  static const _suffix = 'mb';
}

class MessagesId extends Id {
  MessagesId(String value)
      : assert(() {
          final parts = value.split('_');
          return parts.length == 2 && parts[1] == _suffix;
        }(), 'Expected an ID with suffix $_suffix, got $value'),
        super(value);

  factory MessagesId.generate() => MessagesId(z.Id.generate(_suffix));

  static const _suffix = 'ms';
}

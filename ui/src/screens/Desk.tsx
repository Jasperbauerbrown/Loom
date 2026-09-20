// The desk: the always-on conversation, with the now board on the right (03 §13.1).

import { Conversation } from "../components/Conversation";
import { ConversationHeader } from "../components/ConversationHeader";
import { NowBoard } from "../components/NowBoard";

export function Desk({ id }: { id: string }) {
  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <ConversationHeader id={id} />
        <div className="min-h-0 flex-1">
          <Conversation id={id} />
        </div>
      </div>
      <div className="hidden w-80 shrink-0 lg:block">
        <NowBoard />
      </div>
    </div>
  );
}

// Dictation through the browser's own speech recognition - Chrome ships it, it costs
// nothing, and it needs no key, which makes it exactly the right weight for
// simulating a phone call during testing. (In Chrome the audio is transcribed by
// Google's service; a production deployment taking real customer calls would want
// that stated in a privacy note.)
//
// The API is prefixed and absent from TypeScript's lib and from Firefox, so this
// module owns the feature detection and the minimal typings, and the UI simply hides
// the microphone where it cannot work.

interface SpeechResultAlternative {
  transcript: string;
}
interface SpeechResult {
  isFinal: boolean;
  0: SpeechResultAlternative;
}
interface SpeechResultsEvent {
  resultIndex: number;
  results: { length: number; [index: number]: SpeechResult };
}
interface Recognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechResultsEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  start: () => void;
  stop: () => void;
}

type RecognitionCtor = new () => Recognition;

function constructor_(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export const dictationSupported = (): boolean => constructor_() !== null;

export interface Dictation {
  stop: () => void;
}

/**
 * Listen until stopped, streaming text out as it firms up.
 *
 * ``onFinal`` receives finished phrases to append to the note; ``onInterim`` the
 * live guess for showing grey, so the speaker can see they are being heard. Chrome
 * ends recognition on its own after silence, so it is restarted until the caller
 * says stop - otherwise dictation dies mid-thought and the button lies.
 */
export function startDictation(
  onFinal: (text: string) => void,
  onInterim: (text: string) => void,
  onStop: (reason: string) => void,
): Dictation | null {
  const Ctor = constructor_();
  if (!Ctor) return null;

  let wanted = true;
  const recognition = new Ctor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  recognition.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i]!;
      if (result.isFinal) onFinal(result[0].transcript);
      else interim += result[0].transcript;
    }
    onInterim(interim);
  };
  recognition.onerror = (event) => {
    // "no-speech" is just silence; keep listening. Permission problems are final.
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      wanted = false;
      onStop("microphone permission was refused");
    }
  };
  recognition.onend = () => {
    if (wanted) {
      try {
        recognition.start();
      } catch {
        wanted = false;
        onStop("dictation stopped");
      }
    } else {
      onStop("");
    }
  };

  recognition.start();
  return {
    stop: () => {
      wanted = false;
      recognition.stop();
    },
  };
}

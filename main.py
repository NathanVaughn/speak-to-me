import argparse
import io
import json
import os
import shutil
import sys

import peewee as pw
import pydub
from dotenv import load_dotenv
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_watson import DiscoveryV1, SpeechToTextV1

CRED = "ibm-credentials.env"
CONFIDENCE_THRESHOLD = 0.90

DB = pw.SqliteDatabase(None)


class BaseModel(pw.Model):
    class Meta:
        database = DB


class Word(BaseModel):
    text = pw.TextField()
    start = pw.FloatField()
    end = pw.FloatField()
    confidence = pw.FloatField()


def file_names(args):
    class FileData(object):
        pass

    ret = FileData()

    ret.abs_audio_file_name = os.path.abspath(args.audiofile)
    ret.audio_file_dir = os.path.split(ret.abs_audio_file_name)[0]
    ret.audio_file_name = os.path.split(ret.abs_audio_file_name)[1]
    ret.audio_file_title = os.path.splitext(ret.abs_audio_file_name)[0]
    ret.audio_file_extension = os.path.splitext(ret.abs_audio_file_name)[1]

    ret.transcript_file_name = ret.audio_file_title + "-transcript.json"
    ret.abs_transcript_file_name = os.path.abspath(
        os.path.join(ret.audio_file_dir, ret.transcript_file_name)
    )

    ret.database_file_name = ret.audio_file_title + "-database.db"
    ret.abs_database_file_name = os.path.abspath(
        os.path.join(ret.audio_file_dir, ret.database_file_name)
    )

    ret.dict_file_name = ret.audio_file_title + "-dict.txt"
    ret.abs_dict_file_name = os.path.abspath(
        os.path.join(ret.audio_file_dir, ret.dict_file_name)
    )

    if args.script:
        ret.abs_script_file_name = os.path.abspath(args.script)
    if args.output:
        ret.abs_output_file_name = os.path.abspath(args.output)

    return ret


def transcribe(f):
    # check if transcript already exists
    if os.path.isfile(f.abs_transcript_file_name):
        # if so, confirm if we want to continue
        in_ = input("Transcript already exists. Would you like to continue? (y/n) ")
        if in_.lower()[0] == "n":
            sys.exit(0)

    # load credentials
    if not os.path.isfile(CRED):
        raise IOError("Credential file ({}) does not exist.".format(CRED))

    load_dotenv(CRED)
    authenticator = IAMAuthenticator(os.getenv("SPEECH_TO_TEXT_IAM_APIKEY"))

    # speech client
    service = SpeechToTextV1(authenticator=authenticator)
    service.set_service_url(os.getenv("SPEECH_TO_TEXT_URL"))

    print("Transcribing speech. Please wait.")
    with open(abs_file_name, "rb") as file:
        response = service.recognize(
            audio=file,
            model="en-US_BroadbandModel",
            timestamps=True,
            word_confidence=True,
            smart_formatting=True,
        ).get_result()

    with open(f.abs_transcript_file_name, "w") as file:
        json.dump(response, file, indent=4)

    print("Trancription saved to {}".format(f.abs_transcript_file_name))


def process(f):
    if not os.path.isfile(f.abs_transcript_file_name):
        raise IOError("Transcription does not exist.")

    print("Loading transcript data")
    with open(f.abs_transcript_file_name, "r") as file:
        data = json.loads(file.read())

    DB.init(f.abs_database_file_name)
    DB.connect()
    DB.create_tables([Word])

    # load our data into the database
    print("Putting transcript data into database")
    for chunk in data["results"]:
        # grab chunk timestamps and confidence
        chunk_word_timestamps = chunk["alternatives"][0]["timestamps"]
        chunk_word_confidence = chunk["alternatives"][0]["word_confidence"]

        # enter data into our database
        for i in range(len(chunk_word_timestamps)):
            text = chunk_word_timestamps[i][0]
            word_timestamps = chunk_word_timestamps[i][1:3]
            word_confidence = chunk_word_confidence[i][1]

            Word.create(
                text=text.lower(),
                start=word_timestamps[0],
                end=word_timestamps[1],
                confidence=word_confidence,
            )

    print("Processing data")
    # remove any words that fall below confidence threshold
    Word.delete().where(Word.confidence < CONFIDENCE_THRESHOLD).execute()

    # remove any duplicate words and keep the highest confidence one
    word_list = [
        word.text for word in Word.select(Word.text).order_by(Word.text).distinct()
    ]
    for word in word_list:
        # get max confidence for each word
        confidences = Word.select(Word.confidence).where(Word.text == word).execute()
        max_confidence = max([c.confidence for c in confidences])

        # delete anything that does not have max confidence
        Word.delete().where(
            Word.confidence < max_confidence & Word.text == word
        ).execute()


def build_dict(f):
    if not os.path.isfile(f.abs_database_file_name):
        process(f)
    else:
        print("Connecting to existing database")
        DB.init(f.abs_database_file_name)
        DB.connect()

    word_list = [
        word.text for word in Word.select(Word.text).order_by(Word.text).distinct()
    ]

    print("Writing dictionary to {}".format(f.abs_dict_file_name))
    with open(f.abs_dict_file_name, "w") as file:
        for word in word_list:
            file.write(word + "\n")


def speak(f):
    if not os.path.isfile(f.abs_database_file_name):
        process(f)
    else:
        print("Connecting to existing database")
        DB.init(f.abs_database_file_name)
        DB.connect()

    # do checks for processing the audio now
    if not os.path.isfile(f.abs_script_file_name):
        raise IOError("Script does not exist.")

    if not shutil.which("ffmpeg"):
        raise Exception("ffmpeg needs to be installed.")

    # read the script in
    print("Loading script")
    with open(f.abs_script_file_name, "r") as file:
        script_text = file.read().lower().strip()

    print("Loading required word data")
    # figure out all the words we'll need to say
    script_words = script_text.split()
    script_data = []

    # exit if words in the script are not present in the transcript
    word_list = [
        word.text for word in Word.select(Word.text).order_by(Word.text).distinct()
    ]
    missing = list(set(script_words).difference(word_list))
    if missing:
        print("The following words are missing from the source data: ")
        for m in missing:
            print(m)
        sys.exit(1)

    # build data that we need
    for script_word in script_words:
        db_word = Word.get(Word.text == script_word)
        script_data.append(
            {"text": db_word.text, "start": db_word.start, "end": db_word.end}
        )

    # import audio
    print("Building audio data")
    full_audio = None
    for i, item in enumerate(script_data):
        audio = pydub.AudioSegment.from_file(
            f.abs_audio_file_name, format=file_extension[1:]
        )
        # slice and dice
        audio = audio[item["start"] * 1000 : item["end"] * 1000]

        if i == 0:
            full_audio = audio
        else:
            full_audio += audio

    # write output file
    if not f.abs_output_file:
        raise Exception("Output file required")

    print("Writing output file")
    full_audio.export(f.abs_output_file)


def main():
    # build arguments
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--transcribe", action="store_true", help="Transcribe audio file"
    )
    group.add_argument("--dict", action="store_true", help="Create dictionary")
    group.add_argument("--speak", action="store_true", help="Speak text")
    parser.add_argument(
        "--audiofile",
        required=True,
        type=str,
        help="Audio file to transcribe or use as source",
    )
    parser.add_argument("--script", type=str, help="Script to speak")
    parser.add_argument("--output", type=str, help="Output file")
    args = parser.parse_args()

    # check if audio file even exists
    if not os.path.isfile(args.audiofile):
        raise IOError("Audio file does not exist.")

    file_data = file_names(args)

    if args.transcribe:
        transcribe(file_data)
    elif args.dict:
        build_dict(file_data)
    elif args.speak:
        speak(file_data)
    else:
        raise Exception("No mode selected.")

    print("Done!")


if __name__ == "__main__":
    main()

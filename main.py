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
MASTER_DB = pw.SqliteDatabase(":memory:")

# standard Word database model
class Word(pw.Model):
    class Meta:
        database = DB

    text = pw.TextField()
    start = pw.FloatField()
    end = pw.FloatField()
    confidence = pw.FloatField()


# master database Word model
class MasterWord(Word):
    class Meta:
        database = MASTER_DB

    file = pw.TextField()

MASTER_DB.connect()
MASTER_DB.create_tables([MasterWord])


def file_names(args):
    class FileData(object):
        pass

    class AudioFilesData(object):
        pass

    ret = FileData()
    ret.audio_files = []

    for file in args.audiofiles:
        ret2 = AudioFilesData()

        ret2.audio_file_name_abs = os.path.abspath(file)
        ret2.audio_file_dir = os.path.split(ret2.audio_file_name_abs)[0]
        ret2.audio_file_name = os.path.split(ret2.audio_file_name_abs)[1]
        ret2.audio_file_title = os.path.splitext(ret2.audio_file_name)[0]
        ret2.audio_file_extension = os.path.splitext(ret2.audio_file_name_abs)[1]

        ret2.transcript_file_name = ret2.audio_file_title + "-transcript.json"
        ret2.transcript_file_name_abs = os.path.abspath(
            os.path.join(ret2.audio_file_dir, ret2.transcript_file_name)
        )

        ret2.database_file_name = ret2.audio_file_title + "-database.db"
        ret2.database_file_name_abs = os.path.abspath(
            os.path.join(ret2.audio_file_dir, ret2.database_file_name)
        )

        ret.audio_files.append(ret2)

    if args.script:
        ret.script_file_name = args.script
        ret.script_file_name_abs = os.path.abspath(ret.script_file_name)
    if args.output:
        ret.output_file_name = args.output
        ret.output_file_name_abs = os.path.abspath(ret.output_file_name)

    return ret


def transcribe(f):
    """Transcribes a list of audiofiles"""
    # load credentials
    if not os.path.isfile(CRED):
        raise IOError("Credential file {} does not exist.".format(CRED))

    load_dotenv(CRED)
    authenticator = IAMAuthenticator(os.getenv("SPEECH_TO_TEXT_IAM_APIKEY"))

    # speech client
    service = SpeechToTextV1(authenticator=authenticator)
    service.set_service_url(os.getenv("SPEECH_TO_TEXT_URL"))

    for g in f.audio_files:
        # check if transcript already exists
        if os.path.isfile(g.transcript_file_name_abs):
            # if so, confirm if we want to continue
            in_ = input(
                "Transcript of {} already exists. Would you like to continue? (y/n) ".format(
                    g.audio_file_name
                )
            )
            if in_.lower()[0] == "n":
                sys.exit(0)

        print(
            "Transcribing speech of {}. This will take a while.".format(
                g.audio_file_name
            )
        )

        with open(g.audio_file_name_abs, "rb") as file:
            response = service.recognize(
                audio=file,
                model="en-US_BroadbandModel",
                timestamps=True,
                word_confidence=True,
                smart_formatting=True,
            ).get_result()

        with open(g.transcript_file_name_abs, "w") as file:
            json.dump(response, file, indent=4)

        print("Trancription saved to {}".format(g.transcript_file_name))


def build_db(g):
    """Process the transcript of a single file into a database"""
    if not os.path.isfile(g.transcript_file_name_abs):
        raise IOError("Transcription of {} does not exist.".format(g.audio_file_name))

    print("Reading transcript {} data".format(g.transcript_file_name))
    with open(g.transcript_file_name_abs, "r") as file:
        data = json.loads(file.read())

    DB.init(g.database_file_name_abs)
    DB.connect()
    DB.create_tables([Word])

    # load our data into the database
    print(
        "Loading transcript data into {}. This may take a minute.".format(
            g.database_file_name
        )
    )
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


def build_master_db(f):
    """Builds a master in-memory database of multiple transcript databases"""

    print("Creating master database")
    for g in f.audio_files:
        if not os.path.isfile(g.database_file_name_abs):
            build_db(g)
        else:
            print("Connecting to existing database {}".format(g.database_file_name))
            DB.init(g.database_file_name_abs)
            DB.connect()

        print("Loading data from {} into master database".format(g.database_file_name))
        for word in Word.select():
            MasterWord.create(
                text=word.text,
                start=word.start,
                end=word.end,
                confidence=word.confidence,
                file=g.audio_file_name_abs,
            )

    print("Processing master database data")
    # remove any words that fall below confidence threshold
    MasterWord.delete().where(MasterWord.confidence < CONFIDENCE_THRESHOLD).execute()

    # remove any duplicate words and keep the highest confidence one
    word_list = [
        word.text
        for word in MasterWord.select(MasterWord.text)
        .order_by(MasterWord.text)
        .distinct()
    ]
    for word in word_list:
        # get max confidence for each word
        confidences = (
            MasterWord.select(MasterWord.confidence)
            .where(MasterWord.text == word)
            .execute()
        )
        max_confidence = max([c.confidence for c in confidences])

        # delete anything that does not have max confidence
        MasterWord.delete().where(
            Word.confidence < max_confidence & MasterWord.text == word
        ).execute()


def build_dict(f):
    """Builds a dictionary from a list of audiofiles"""
    if not hasattr(f, "abs_output_file_name"):
        raise Exception("Output argument missing")

    build_master_db(f)

    word_list = [
        word.text
        for word in MasterWord.select(MasterWord.text)
        .order_by(MasterWord.text)
        .distinct()
    ]

    print("Writing dictionary to {}".format(f.abs_output_file_name))
    with open(f.abs_output_file_name, "w") as file:
        for word in word_list:
            file.write(word + "\n")


def speak(f):
    """Builds desired phrase from a list of audiofiles"""

    # checks
    if not hasattr(f, "output_file_name_abs"):
        raise Exception("Output file argument missing")

    if not hasattr(f, "script_file_name_abs"):
        raise Exception("Script file argument missing")

    if not os.path.isfile(f.script_file_name_abs):
        raise IOError("Script file does not exist.")

    if not shutil.which("ffmpeg"):
        raise Exception("ffmpeg needs to be installed.")

    build_master_db(f)

    # read the script in
    print("Loading script")
    with open(f.script_file_name_abs, "r") as file:
        script_text = file.read().lower().strip()

    print("Loading required word data")
    # figure out all the words we'll need to say
    script_words = script_text.split()
    script_data = []

    # exit if words in the script are not present in the transcript
    word_list = [
        word.text
        for word in MasterWord.select(MasterWord.text)
        .order_by(MasterWord.text)
        .distinct()
    ]
    missing = list(set(script_words).difference(word_list))

    if missing:
        print("The following words are missing from the source data: ")
        for m in missing:
            print(m)
        sys.exit(1)

    # build data that we need
    for script_word in script_words:
        script_data.append(MasterWord.get(MasterWord.text == script_word))

    # import audio
    print("Building audio data")
    full_audio = None
    for i, item in enumerate(script_data):
        audio = pydub.AudioSegment.from_file(
            item.file, format=os.path.splitext(item.file)[1][1:]
        )
        # slice and dice
        audio = audio[item.start * 1000 : item.end * 1000]

        if i == 0:
            full_audio = audio
        else:
            full_audio += audio

    print("Saving output audio to {}".format(f.output_file_name))
    full_audio.export(f.output_file_name_abs)


def main():
    # build arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["transcribe", "dict", "speak"], help="Mode to run")
    parser.add_argument(
        "--audiofiles",
        required=True,
        type=str,
        help="Audio file(s) to use as source",
        nargs="+",
    )
    parser.add_argument("--script", type=str, help="Script to speak")
    parser.add_argument("--output", type=str, help="Output file")
    args = parser.parse_args()

    # check if audio files even exists
    for file in args.audiofiles:
        if not os.path.isfile(file):
            raise IOError("Audio file does not exist.")

    file_data = file_names(args)

    if args.mode == "transcribe":
        transcribe(file_data)
    elif args.mode == "dict":
        build_dict(file_data)
    elif args.mode == "speak":
        speak(file_data)
    else:
        raise Exception("No mode selected.")

    print("Done!")


if __name__ == "__main__":
    main()

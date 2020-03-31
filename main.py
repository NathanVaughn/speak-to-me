import argparse
import copy
import json
import os
import shutil
import sys

import peewee as pw
import pydub
from dotenv import load_dotenv
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_watson import SpeechToTextV1

CRED = "ibm-credentials.env"

CONFIDENCE_THRESHOLD = 0.90
VALID_EXTENSIONS = [".mp3", ".wav", ".ogg", ".flac"]
# how many milliseconds to shave off each side of a word
TIGHTNESS = 0

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


# connect to the master database since we'll more than likley need it
MASTER_DB.connect()
MASTER_DB.create_tables([MasterWord])


def file_names(args):
    # function to build all the file paths and names we need
    class FileData(object):
        # placeholder class so we can access data in an object syntax
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
        raise Exception("Credential file {} does not exist.".format(CRED))

    load_dotenv(CRED)
    authenticator = IAMAuthenticator(os.getenv("SPEECH_TO_TEXT_IAM_APIKEY"))

    # speech client
    service = SpeechToTextV1(authenticator=authenticator)
    service.set_service_url(os.getenv("SPEECH_TO_TEXT_URL"))

    for g in f.audio_files:
        if g.audio_file_extension not in VALID_EXTENSIONS:
            raise Exception(
                "Audio file {} is an invalid format".format(g.audio_file_name)
            )

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

        # final sanity check as this code can cost real money
        length_sec = pydub.AudioSegment.from_file(
            g.audio_file_name_abs, format=g.audio_file_extension[1:]
        ).duration_seconds

        in_ = input(
            "Are you sure you want to transcribe {} min, {} sec of audio? (y/n) ".format(
                round(length_sec / 60), round(length_sec % 60)
            )
        )
        if in_.lower()[0] == "n":
            sys.exit(0)

        print(
            "Transcribing speech of {}. This will take a while.".format(
                g.audio_file_name
            )
        )

        # send the audio to Watson
        with open(g.audio_file_name_abs, "rb") as file:
            response = service.recognize(
                audio=file,
                model="en-US_BroadbandModel",
                timestamps=True,
                word_confidence=True,
                smart_formatting=True,
            ).get_result()

        # write out the resulting transcript
        with open(g.transcript_file_name_abs, "w") as file:
            json.dump(response, file, indent=4)

        print("Trancription saved to {}".format(g.transcript_file_name))


def build_db(g):
    """Process the transcript of a single file into a database"""
    if not os.path.isfile(g.transcript_file_name_abs):
        raise Exception("Transcription of {} does not exist.".format(g.audio_file_name))

    print("Reading transcript {} data".format(g.transcript_file_name))
    with open(g.transcript_file_name_abs, "r") as file:
        data = json.loads(file.read())

    # initialize the db and connect to it
    DB.init(g.database_file_name_abs)
    DB.connect()
    DB.create_tables([Word])

    # load our data into the database
    print("Loading transcript data into {}.".format(g.database_file_name))

    new_items = []

    for chunk in data["results"]:
        # grab chunk timestamps and confidence
        chunk_word_timestamps = chunk["alternatives"][0]["timestamps"]
        chunk_word_confidence = chunk["alternatives"][0]["word_confidence"]

        # enter data into our database
        for i in range(len(chunk_word_timestamps)):
            text = chunk_word_timestamps[i][0]
            word_timestamps = chunk_word_timestamps[i][1:3]
            word_confidence = chunk_word_confidence[i][1]

            # prepare items in a list for batch insert
            new_items.append(
                Word(
                    text=text.lower(),
                    start=word_timestamps[0],
                    end=word_timestamps[1],
                    confidence=word_confidence,
                )
            )

    # batch insert
    with DB.atomic():
        Word.bulk_create(new_items, batch_size=100)


def build_master_db(f):
    """Builds a master in-memory database of multiple transcript databases"""

    print("Creating master database")
    new_items = []

    for g in f.audio_files:
        if not os.path.isfile(g.database_file_name_abs):
            build_db(g)
        else:
            print("Connecting to existing database {}".format(g.database_file_name))
            DB.init(g.database_file_name_abs)
            DB.connect()

        print("Loading data from {} into master database".format(g.database_file_name))
        # just moving data from one database to another and adding a filename field
        # prepare items in a list for batch insert
        new_items.extend(
            [
                MasterWord(
                    text=word.text,
                    start=word.start,
                    end=word.end,
                    confidence=word.confidence,
                    file=g.audio_file_name_abs,
                )
                for word in Word.select()
            ]
        )

    # batch insert
    with DB.atomic():
        MasterWord.bulk_create(new_items, batch_size=100)

    print("Processing master database data")

    print(
        "Removing words that fall below the confidence threshold of {}".format(
            CONFIDENCE_THRESHOLD
        )
    )
    # remove any words that fall below confidence threshold
    MasterWord.delete().where(MasterWord.confidence < CONFIDENCE_THRESHOLD).execute()

    print("Removing duplicate words and keeping the one with highest confidence")
    # remove any duplicate words and keep the highest confidence one

    unique_words = MasterWord.select(MasterWord.text).distinct()
    for word in unique_words:
        # get max confidence for each word
        max_confidence = (
            MasterWord.select(MasterWord.confidence)
            .where(MasterWord.text == word.text)
            .order_by(MasterWord.confidence.desc())
            .execute()[0]
            .confidence
        )

        # delete anything that does not have max confidence
        MasterWord.delete().where(
            Word.confidence < max_confidence & MasterWord.text == word.text
        ).execute()


def build_dict(f):
    """Builds a dictionary from a list of audiofiles"""
    if not hasattr(f, "output_file_name_abs"):
        raise Exception("Output argument missing")

    build_master_db(f)

    word_list = [
        word.text
        for word in MasterWord.select(MasterWord.text)
        .order_by(MasterWord.text)
        .distinct()
    ]

    print("Writing dictionary to {}".format(f.output_file_name_abs))
    with open(f.output_file_name_abs, "w") as file:
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
        raise Exception("Script file does not exist.")

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

    # get word list
    word_list = [
        word.text
        for word in MasterWord.select(MasterWord.text)
        .order_by(MasterWord.text)
        .distinct()
    ]
    missing = list(set(script_words).difference(word_list))

    # exit if words in the script are not present in the transcript
    if missing:
        print("The following words are missing from the source data: ")
        for m in missing:
            print(m)
        sys.exit(1)

    # build data that we need
    for script_word in script_words:
        script_data.append(MasterWord.get(MasterWord.text == script_word))

    # build output audio
    print("Building output audio")
    # placeholder for full audio segment
    full_audio = pydub.AudioSegment.empty()
    # dict to cache audio segment objects in memory for performance
    audio_segments = {}

    for i, item in enumerate(script_data):
        # print("Working on word {}: {}".format(i, item.text))

        # if file we need segment from is not cached, add it to cache
        if item.file not in audio_segments.keys():
            audio_segments[item.file] = pydub.AudioSegment.from_file(
                item.file, format=os.path.splitext(item.file)[1][1:]
            )

        # make a copy of the segment from cache to not effect original
        audio = copy.deepcopy(audio_segments[item.file])
        # slice and dice
        audio = audio[(item.start * 1000) + TIGHTNESS : (item.end * 1000) - TIGHTNESS]
        # combine
        full_audio += audio

    # normalize levels
    full_audio = pydub.effects.normalize(full_audio)

    print("Saving output audio to {}".format(f.output_file_name))
    full_audio.export(f.output_file_name_abs)


def main():
    # build arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=["transcribe", "dict", "speak"], help="Mode to run"
    )
    parser.add_argument(
        "audiofiles", type=str, help="Audio file(s) to use as source", nargs="+",
    )
    parser.add_argument("--script", type=str, help="Script to speak")
    parser.add_argument("--output", type=str, help="Output file")
    args = parser.parse_args()

    # check if audio files even exists
    for file in args.audiofiles:
        if not os.path.isfile(file):
            raise Exception("Audio file {} does not exist.".format(file))

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

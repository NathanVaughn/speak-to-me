# Speak-to-me

Python program that takes an audio clip, transcribes it, and can re-arrange words

# Usage

## Transcribe

To transcribe audio, you'll need to create an [IBM Cloud](https://cloud.ibm.com)
account. Enable the
[Speech To Text](https://cloud.ibm.com/catalog/services/speech-to-text) service.
Go to your [resource page](https://cloud.ibm.com/resources), select your Speech To Text
resource, download the credentials file, and place it in the project directory.

```bash
python main.py --audiofile data/myfile.mp3 --transcribe
```

This will send the audio to the Watson Speech To Text service, and write out the
resulting transcript to `{audiofilename}-transcript.json`.

## Create Dictionary

```bash
python main.py --audiofile data/myfile.mp3 --dict
```

This will create a file (`{audiofilename}-dict.txt`) with a list of words from the
transcript above the confidence threshold.

## Speak

```bash
python main.py --audiofile data/myfile.mp3 --speak --script script.txt --output output.mp3
```

This will read the given script and generate a new output audio file from the transcript file.
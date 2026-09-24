API documentation
http://127.0.0.1:7860/

API Recorder

28 API endpoints


Choose one of the following ways to interact with the API.

python javascript cURL MCP

Here is the python version of the API.

1. Install the python client (docs) if you don't already have it installed.

copy
$ pip install gradio_client
2. Find the API endpoint below corresponding to your desired function in the app. Copy the code snippet, replacing the placeholder values with your own input data. Or use the 
API Recorder

 to automatically generate your API requests.

API name: /rainfall_gen_zero_shot
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_text="注意看，这个男人叫小帅",
			prompt_wav=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			prompt_text="",
			speed=1,
			output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			output_file_name="",
			single_file_suffix="wav",
			api_name="/rainfall_gen_zero_shot"
	)
	print(result)
Accepts 7 parameters:
input_text str Default: "注意看，这个男人叫小帅"

The input value that is provided in the "待处理文本" Textbox component.

prompt_wav filepath Required

The input value that is provided in the "音色文件(最长不要超过30秒)" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

prompt_text str Default: ""

The input value that is provided in the "提示语音对应的文本(支持自动识别，支持手动修改)" Textbox component.

speed float Default: 1

The input value that is provided in the "语速" Slider component.

output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

output_file_name str Default: ""

The input value that is provided in the "保存文件名[为空则根据时间戳自动生成,不需要后缀]" Textbox component.

single_file_suffix Literal['wav'] Default: "wav"

The input value that is provided in the "文件格式[后缀名]" Dropdown component.

Returns 1 element
filepath

The output value that appears in the "生成结果预览" Audio component.

API name: /prompt_wav_recognition
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "音色文件(最长不要超过30秒)" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "提示语音对应的文本(支持自动识别，支持手动修改)" Textbox component.

API name: /refresh_sft_audio_list
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			api_name="/refresh_sft_audio_list"
	)
	print(result)
Accepts 0 parameters:
Returns 1 element
Literal['开拓者']

The output value that appears in the "选择预训练音色" Dropdown component.

API name: /rainfall_gen_sft
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_text="注意看，这个男人叫小帅",
			sft_dropdown="开拓者",
			speed=1,
			output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			output_file_name="",
			single_file_suffix="wav",
			api_name="/rainfall_gen_sft"
	)
	print(result)
Accepts 6 parameters:
input_text str Default: "注意看，这个男人叫小帅"

The input value that is provided in the "待处理文本" Textbox component.

sft_dropdown Literal['开拓者'] Default: "开拓者"

The input value that is provided in the "选择预训练音色" Dropdown component.

speed float Default: 1

The input value that is provided in the "语速" Slider component.

output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

output_file_name str Default: ""

The input value that is provided in the "保存文件名[为空则根据时间戳自动生成,不需要后缀]" Textbox component.

single_file_suffix Literal['wav'] Default: "wav"

The input value that is provided in the "文件格式[后缀名]" Dropdown component.

Returns 1 element
filepath

The output value that appears in the "合成音频" Audio component.

API name: /refresh_sft_audio_list_1
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			api_name="/refresh_sft_audio_list_1"
	)
	print(result)
Accepts 0 parameters:
Returns 1 element
Literal['开拓者']

The output value that appears in the "选择预训练音色" Dropdown component.

API name: /rainfall_gen_instruct
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_text="注意看，这个男人叫小帅",
			sft_dropdown="开拓者",
			instruct_text="湖南话",
			speed=1,
			output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			output_file_name="",
			single_file_suffix="wav",
			api_name="/rainfall_gen_instruct"
	)
	print(result)
Accepts 7 parameters:
input_text str Default: "注意看，这个男人叫小帅"

The input value that is provided in the "待处理文本" Textbox component.

sft_dropdown Literal['开拓者'] Default: "开拓者"

The input value that is provided in the "选择预训练音色" Dropdown component.

instruct_text str Default: "湖南话"

The input value that is provided in the "指令文本" Textbox component.

speed float Default: 1

The input value that is provided in the "语速" Slider component.

output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

output_file_name str Default: ""

The input value that is provided in the "保存文件名[为空则根据时间戳自动生成,不需要后缀]" Textbox component.

single_file_suffix Literal['wav'] Default: "wav"

The input value that is provided in the "文件格式[后缀名]" Dropdown component.

Returns 1 element
filepath

The output value that appears in the "合成音频" Audio component.

API name: /rainfall_quick_multi_role_text_generate
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			role_name1="",
			role_audio1=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt1="",
			role_name2="",
			role_audio2=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt2="",
			role_name3="",
			role_audio3=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt3="",
			role_name4="",
			role_audio4=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt4="",
			role_name5="",
			role_audio5=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt5="",
			role_name6="",
			role_audio6=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt6="",
			role_name7="",
			role_audio7=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt7="",
			role_name8="",
			role_audio8=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt8="",
			role_name9="",
			role_audio9=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt9="",
			role_name10="",
			role_audio10=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			role_txt10="",
			multi_role_text="Hello!!",
			multi_output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			multi_file_name="",
			multi_file_suffix="wav",
			multi_interval=0.1,
			api_name="/rainfall_quick_multi_role_text_generate"
	)
	print(result)
Accepts 35 parameters:
role_name1 str Default: ""

The input value that is provided in the "角色1名称" Textbox component.

role_audio1 filepath Required

The input value that is provided in the "角色1音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt1 str Default: ""

The input value that is provided in the "角色1文本" Textbox component.

role_name2 str Default: ""

The input value that is provided in the "角色2名称" Textbox component.

role_audio2 filepath Required

The input value that is provided in the "角色2音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt2 str Default: ""

The input value that is provided in the "角色2文本" Textbox component.

role_name3 str Default: ""

The input value that is provided in the "角色3名称" Textbox component.

role_audio3 filepath Required

The input value that is provided in the "角色3音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt3 str Default: ""

The input value that is provided in the "角色3文本" Textbox component.

role_name4 str Default: ""

The input value that is provided in the "角色4名称" Textbox component.

role_audio4 filepath Required

The input value that is provided in the "角色4音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt4 str Default: ""

The input value that is provided in the "角色4文本" Textbox component.

role_name5 str Default: ""

The input value that is provided in the "角色5名称" Textbox component.

role_audio5 filepath Required

The input value that is provided in the "角色5音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt5 str Default: ""

The input value that is provided in the "角色5文本" Textbox component.

role_name6 str Default: ""

The input value that is provided in the "角色6名称" Textbox component.

role_audio6 filepath Required

The input value that is provided in the "角色6音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt6 str Default: ""

The input value that is provided in the "角色6文本" Textbox component.

role_name7 str Default: ""

The input value that is provided in the "角色7名称" Textbox component.

role_audio7 filepath Required

The input value that is provided in the "角色7音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt7 str Default: ""

The input value that is provided in the "角色7文本" Textbox component.

role_name8 str Default: ""

The input value that is provided in the "角色8名称" Textbox component.

role_audio8 filepath Required

The input value that is provided in the "角色8音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt8 str Default: ""

The input value that is provided in the "角色8文本" Textbox component.

role_name9 str Default: ""

The input value that is provided in the "角色9名称" Textbox component.

role_audio9 filepath Required

The input value that is provided in the "角色9音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt9 str Default: ""

The input value that is provided in the "角色9文本" Textbox component.

role_name10 str Default: ""

The input value that is provided in the "角色10名称" Textbox component.

role_audio10 filepath Required

The input value that is provided in the "角色10音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

role_txt10 str Default: ""

The input value that is provided in the "角色10文本" Textbox component.

multi_role_text str Required

The input value that is provided in the "用户输入" Textbox component.

multi_output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

multi_file_name str Default: ""

The input value that is provided in the "保存文件名[为空则根据时间戳自动生成,不需要后缀]" Textbox component.

multi_file_suffix Literal['wav'] Default: "wav"

The input value that is provided in the "文件格式[后缀名]" Dropdown component.

multi_interval float Default: 0.1

The input value that is provided in the "对话间隔[单位是秒]" Slider component.

Returns 1 element
filepath

The output value that appears in the "多角色对话生成结果" Audio component.

API name: /prompt_wav_recognition_1
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_1"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色1音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色1文本" Textbox component.

API name: /prompt_wav_recognition_2
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_2"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色2音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色2文本" Textbox component.

API name: /prompt_wav_recognition_3
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_3"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色3音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色3文本" Textbox component.

API name: /prompt_wav_recognition_4
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_4"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色4音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色4文本" Textbox component.

API name: /prompt_wav_recognition_5
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_5"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色5音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色5文本" Textbox component.

API name: /prompt_wav_recognition_6
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_6"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色6音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色6文本" Textbox component.

API name: /prompt_wav_recognition_7
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_7"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色7音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色7文本" Textbox component.

API name: /prompt_wav_recognition_8
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_8"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色8音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色8文本" Textbox component.

API name: /prompt_wav_recognition_9
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_9"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色9音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色9文本" Textbox component.

API name: /prompt_wav_recognition_10
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/prompt_wav_recognition_10"
	)
	print(result)
Accepts 1 parameter:
audio_path filepath Required

The input value that is provided in the "角色10音色" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "角色10文本" Textbox component.

API name: /on_click_save_sft_voice
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			audio_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			sft_name="示例名称",
			api_name="/on_click_save_sft_voice"
	)
	print(result)
Accepts 2 parameters:
audio_path filepath Required

The input value that is provided in the "音色文件(最长不要超过30秒)" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

sft_name str Default: "示例名称"

The input value that is provided in the "文件名称(不要带任何后缀)" Textbox component.

Returns 1 element
API name: /do_tokenizer_encode_click
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_str="雨落大模型",
			api_name="/do_tokenizer_encode_click"
	)
	print(result)
Accepts 1 parameter:
input_str str Default: "雨落大模型"

The input value that is provided in the "待编码内容" Textbox component.

Returns 1 element
str

The output value that appears in the "value_215" Html component.

API name: /do_text_normalizer_click
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_str="结果是x-2",
			api_name="/do_text_normalizer_click"
	)
	print(result)
Accepts 1 parameter:
input_str str Default: "结果是x-2"

The input value that is provided in the "待处理文本" Textbox component.

Returns 1 element
str

The output value that appears in the "value_225" Markdown component.

API name: /do_speech_token_click
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_str=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/do_speech_token_click"
	)
	print(result)
Accepts 1 parameter:
input_str filepath Required

The input value that is provided in the "音色文件(最长不要超过30秒)" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "value_244" Html component.

API name: /extract_from_video
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			video_path="resources/prompt_video/rainfall_model_test.wmv",
			audio_path="outputs/rainfall_model_test.wav",
			api_name="/extract_from_video"
	)
	print(result)
Accepts 2 parameters:
video_path str Default: "resources/prompt_video/rainfall_model_test.wmv"

The input value that is provided in the "视频文件路径" Textbox component.

audio_path str Default: "outputs/rainfall_model_test.wav"

The input value that is provided in the "提取的音频路径" Textbox component.

Returns 1 element
API name: /batch_file_change_harmony
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			files=[handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/sample_file.pdf')],
			batch_change_speed_output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			n_steps=3,
			delay_ms=30,
			suffix="",
			api_name="/batch_file_change_harmony"
	)
	print(result)
Accepts 5 parameters:
files list[filepath] Required

The input value that is provided in the "上传多个文件" File component. null

batch_change_speed_output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

n_steps float Default: 3

The input value that is provided in the "音高偏移量(每次调整一个半音)" Slider component.

delay_ms float Default: 30

The input value that is provided in the "延迟时间(单位是毫秒)" Slider component.

suffix str Default: ""

The input value that is provided in the "批量文件名标识[为空则使用下划线加harmony加各参数数字，比如hi.wav变成hi_harmony_3_30.wav]" Textbox component.

Returns 1 element
API name: /batch_file_change_reverb
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			files=[handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/sample_file.pdf')],
			batch_change_speed_output_dir="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs",
			rt60=0.8,
			suffix="",
			api_name="/batch_file_change_reverb"
	)
	print(result)
Accepts 4 parameters:
files list[filepath] Required

The input value that is provided in the "上传多个文件" File component. null

batch_change_speed_output_dir str Default: "E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall/outputs"

The input value that is provided in the "保存路径" Textbox component.

rt60 float Default: 0.8

The input value that is provided in the "混响时间(声音衰减60分贝所需时间,单位是秒)" Slider component.

suffix str Default: ""

The input value that is provided in the "批量文件名标识[为空则使用下划线加reverb加各参数数字，比如hi.wav变成hi_reverb_0.8.wav]" Textbox component.

Returns 1 element
API name: /convert_audio_format 音频格式转换工具
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_file="",
			output_format="mp3",
			api_name="/convert_audio_format"
	)
	print(result)
Accepts 2 parameters:
input_file str Default: ""

The input value that is provided in the "原始音频文件" Textbox component.

output_format Literal['wav', 'mp3', 'flac', 'ogg'] Default: "mp3"

The input value that is provided in the "文件格式" Dropdown component.

Returns 1 element
API name: /concat_wavs_complete_torchaudio 使用torchaudio的完整WAV文件拼接函数
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_file1="",
			input_file2="",
			output_file="",
			target_sample_rate="24000",
			target_channels="1",
			normalize_volume="false",
			api_name="/concat_wavs_complete_torchaudio"
	)
	print(result)
Accepts 6 parameters:
input_file1 str Default: ""

The input value that is provided in the "待拼接的wav文件1" Textbox component.

input_file2 str Default: ""

The input value that is provided in the "待拼接的wav文件2" Textbox component.

output_file str Default: ""

The input value that is provided in the "拼接后的wav文件名" Textbox component.

target_sample_rate str Default: "24000"

The input value that is provided in the "拼接后采样率" Textbox component.

target_channels str Default: "1"

The input value that is provided in the "拼接后声道数" Textbox component.

normalize_volume Literal['True', 'False'] Default: "false"

The input value that is provided in the "是否音量归一化" Dropdown component.

Returns 1 element
API name: /crop_wav_wave 使用wave库裁剪WAV文件
copy
from gradio_client import Client

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			input_file="",
			output_file="",
			start_time="0.0",
			end_time="1.0",
			api_name="/crop_wav_wave"
	)
	print(result)
Accepts 4 parameters:
input_file str Default: ""

The input value that is provided in the "待裁剪的wav文件" Textbox component.

output_file str Default: ""

The input value that is provided in the "裁剪后的wav文件名" Textbox component.

start_time str Default: "0.0"

The input value that is provided in the "裁剪开始时间(秒数)" Textbox component.

end_time str Default: "1.0"

The input value that is provided in the "裁剪结束时间(秒数)" Textbox component.

Returns 1 element
API name: /get_audio_metadata 获取音频文件的详细元数据
copy
from gradio_client import Client, handle_file

	client = Client("http://127.0.0.1:7860/")
	result = client.predict(
			file_path=handle_file('https://github.com/gradio-app/gradio/raw/main/test/test_files/audio_sample.wav'),
			api_name="/get_audio_metadata"
	)
	print(result)
Accepts 1 parameter:
file_path filepath Required

The input value that is provided in the "音频文件" Audio component. The FileData class is a subclass of the GradioModel class that represents a file object within a Gradio interface. It is used to store file data and metadata when a file is uploaded. Attributes: path: The server file path where the file is stored. url: The normalized server URL pointing to the file. size: The size of the file in bytes. orig_name: The original filename before upload. mime_type: The MIME type of the file. is_stream: Indicates whether the file is a stream. meta: Additional metadata used internally (should not be changed).

Returns 1 element
str

The output value that appears in the "value_338" Markdown component.
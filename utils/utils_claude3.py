import anthropic, boto3, botocore, os, random, pprint
from openai import OpenAI
import time, json
from copy import deepcopy
from botocore.exceptions import ClientError
from utils.key import OPENAI_API_KEY

SLEEP_ON_THROTTLING_SEC = 5

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _is_openai(modelId):
    return modelId.startswith('gpt-')


def _is_titan(modelId):
    # True for provisioned models (assuming to be fine-tuned Titan) or Titan.
    return modelId.startswith('arn:aws:bedrock') or modelId.startswith('amazon.titan')

def _is_claude3_sonnet(modelId):
    # True for provisioned models (assuming to be fine-tuned Titan) or Titan.
    return modelId.startswith('arn:aws:bedrock') or modelId.startswith('anthropic.claude-3-sonnet')

def _is_claude3(modelId):
    # True for provisioned models (assuming to be fine-tuned Titan) or Titan.
    return modelId.startswith('arn:aws:bedrock') or modelId.startswith('anthropic.claude-3')


# This internal method will include arbitrary long input that is designed to generate an extremely long model output
def _get_prompt_template(num_input_tokens, modelId):
    # Determine the service based on modelId prefix

    fillers=''
    i = num_input_tokens - 1
    i += 1 if _is_titan(modelId) else 0
    for l in range(i):
        fillers += random.choice(['hello', 'world', 'foo', 'bar']) + ' '
        
    tokens = ''
    if _is_openai(modelId):
        pass
    elif _is_claude3(modelId):
        pass
    elif _is_titan(modelId):
        tokens += 'Human: '
    else:
        tokens += 'Human: '
    tokens += f'Ignore X ' + f'<X>{fillers}</X>\n'

    if _is_titan(modelId):
        # This task prompt generates around 3K tokens out
        tokens += 'Task: write a long speech about each of the 50 most important issues of the world. Go into details about each problem with history background and figures involved.'
    else:
        tokens += 'Task: Print numbers from 1 to 9999 as words. Continue listing the numbers in word format until the space runs out. \n'
        if _is_openai(modelId):
            tokens += 'one two three'
        elif _is_claude3(modelId):
            tokens += 'one two three'
        else:
            tokens += '\n\nAssistant:one two three '  # model will continue with "four five..."
    return tokens


def _construct_req(modelId, prompt, max_tokens_to_sample, temperature, accept, contentType, stream):
    """
    Private method to construct the body for model invocation based on the model type.
    """
    # OpenAI Models
    if _is_openai(modelId):
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "model": modelId,
            "max_tokens": max_tokens_to_sample,
            "temperature": temperature,
            "stream": stream,
        }
    # Anthropic Models Claude3
    elif _is_claude3(modelId):
        req = {
            "body" : json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens_to_sample,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": prompt
                    }]
                }],
                "temperature": temperature,
                "top_p": 0.9,  # Example value, adjust as needed
                #"stop_sequences": ["\n\nHuman:"]
            }),
            "accept": accept,
            "contentType" : contentType,
            "modelId" : modelId,
        }
    # Anthropic Models
    elif modelId.startswith('anthropic.'):
        req = {
            "body" : json.dumps({
                "prompt": prompt,
                "max_tokens_to_sample": max_tokens_to_sample,
                "temperature": temperature,
                "top_p": 0.9  # Example value, adjust as needed
            }),
            "accept": accept,
            "contentType" : contentType,
            "modelId" : modelId,
        }
    # Titan models
    elif _is_titan(modelId):
        req = {
            "body" : json.dumps({
                "inputText": prompt,
                "textGenerationConfig": {
                    "maxTokenCount": max_tokens_to_sample,
                    "stopSequences": [],
                    "temperature": temperature,  
                    "topP": 0.9
                }
            }),
            "accept": accept,
            "contentType" : contentType,
            "modelId" : modelId,
        }
    # A2I Models
    elif modelId.startswith('ai21.'):
        req = {
            "body" : json.dumps({
                "prompt": prompt,
                "maxTokens": max_tokens_to_sample,
                "temperature": temperature,
                "topP": 0.5  # Example value, adjust as needed
            }),
            "accept": accept,
            "contentType" : contentType,
            "modelId" : modelId,
        }
    else:
        assert f"ERROR: modelId = {modelId} is not supported yet!"

    return req

''' 
This method creates a prompt of input length `expected_num_tokens` which instructs the LLM to generate extremely long model resopnse
'''
anthropic_client = anthropic.Anthropic() # used to count tokens only
def create_prompt(expected_num_tokens, modelId):
    logger.log(logging.DEBUG, f"create_prompt called with modelId: {modelId}")
    num_tokens_in_prompt_template = anthropic_client.count_tokens(_get_prompt_template(0, modelId))
    #print(f"num_tokens_in_prompt_template:{num_tokens_in_prompt_template}")
    additional_tokens_needed = max(expected_num_tokens - num_tokens_in_prompt_template,0)
    #print(f"additional_tokens_needed:{additional_tokens_needed}")
    logger.log(logging.DEBUG, f'expected_num_tokens={expected_num_tokens}, num_tokens_in_prompt_template={num_tokens_in_prompt_template}, additional_tokens_needed={additional_tokens_needed}')
    
    prompt_template = _get_prompt_template(additional_tokens_needed, modelId)
    #print(f"prompt_template:{prompt_template}")
    actual_num_tokens = anthropic_client.count_tokens(prompt_template)
    #print(f"actual_num_tokens:{actual_num_tokens}")
    logger.log(logging.DEBUG, f'expected_num_tokens={expected_num_tokens}, actual_tokens={actual_num_tokens}')
    assert expected_num_tokens==actual_num_tokens, f'Failed to generate prompt at required length: expected_num_tokens={expected_num_tokens} != actual_num_tokens={actual_num_tokens}'
    
    return prompt_template


def _send_request(client, modelId, req, stream):
    
    if _is_openai(modelId):
        response = client.chat.completions.create(**req)
    else:
        if stream:
            response = client.invoke_model_with_response_stream(**req)
        else:
            response = client.invoke_model(**req)
    return response
 
def consume_openai_stream(response):
    first_byte = None
    stop_reason = None
    for chunk in response:
        if not first_byte: 
            first_byte = time.time() # update the time to first byte
        if chunk.choices[0].finish_reason is not None:
            stop_reason = chunk.choices[0].finish_reason
    return first_byte, stop_reason


def consume_bedrock_stream(response):
    first_byte = None
    stop_reason = None
    event_stream = response.get('body')
    for event in event_stream:
        if not first_byte: 
            first_byte = time.time() # update the time to first byte
        chunk = event.get('chunk')
        if chunk:
            # end of stream - check stop_reason in last chunk
            chunk_json = json.loads(chunk.get('bytes').decode())
            if 'stop_reason' in chunk_json:
                stop_reason = chunk_json['stop_reason']
            if 'completionReason' in chunk_json:
                stop_reason = chunk_json['completionReason']
    return first_byte, stop_reason

def consume_bedrock_stream_new(response):
    first_byte = None
    last_byte = None
    stop_reason = None
    event_stream = response.get('body')
    for event in event_stream:
        if not first_byte: 
            first_byte = time.time() # update the time to first byte
        chunk = event.get('chunk')
        if chunk:
            # end of stream - check stop_reason in last chunk
            chunk_json = json.loads(chunk.get('bytes').decode())
            if 'stop_reason' in chunk_json:
                stop_reason = chunk_json['stop_reason']
                #print(f"stop_reason:{stop_reason}")
                if(stop_reason=='max_tokens') or (stop_reason=='stop_sequence'):
                    first_byte = float(chunk_json['amazon-bedrock-invocationMetrics']['firstByteLatency'])/1000
                    last_byte = float(chunk_json['amazon-bedrock-invocationMetrics']['invocationLatency'])/1000
            if 'completionReason' in chunk_json:
                stop_reason = chunk_json['completionReason']
    return first_byte, last_byte, stop_reason

def consume_bedrock_claude3_stream(response):
    first_byte = None
    last_byte = None
    stop_reason = None
    event_stream = response.get('body')
    chunk_json = {}
    for event in event_stream:
        chunk = event.get('chunk')
        if chunk:
            # end of stream - check stop_reason in last chunk
            chunk_json = json.loads(chunk.get('bytes').decode())
            if chunk_json['type']=="message_delta":
                stop_reason = chunk_json['delta']['stop_reason']
            # if 'completionReason' in chunk_json:
            #     stop_reason = chunk_json['completionReason']
    if(chunk_json['type']=='message_stop'):
        first_byte = float(chunk_json['amazon-bedrock-invocationMetrics']['firstByteLatency'])/1000
        last_byte = float(chunk_json['amazon-bedrock-invocationMetrics']['invocationLatency'])/1000
    return first_byte,last_byte, stop_reason
'''
This method will invoke the model, possibly in streaming mode,
In case of throttling error, the method will retry. Throttling and related sleep time isn't measured.
The method ensures the response includes `max_tokens_to_sample` by verify the stop_reason is `max_tokens`

client - the bedrock runtime client to invoke the model
modelId - the model id to invoke
prompt - the prompt to send to the model
max_tokens_to_sample - the number of tokens to sample from the model's response
stream - whether to invoke the model in streaming mode
temperature - the temperature to use for sampling the model's response

Returns the time to first byte, last byte, and invocation time as iso8601 (seconds)
'''
def benchmark(client, modelId, prompt, max_tokens_to_sample, stream=True, temperature=0):
    import time
    from datetime import datetime
    import pytz
    accept = 'application/json'
    contentType = 'application/json'
    req = _construct_req(modelId, prompt, max_tokens_to_sample, temperature, accept, contentType, stream)
    logger.log(logging.DEBUG, f'req={req}')
   
    while True:
        try:
            start = time.time()
            first_byte = None
            dt = datetime.fromtimestamp(start, tz=pytz.utc)
            invocation_timestamp_iso = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            
            response = _send_request(client, modelId, req, stream)
                        
            if not stream:
                logger.log(logging.DEBUG, f'response={response}')
                if _is_openai(modelId):
                    response_body = response.choices[0].message.content
                    stop_reason = response.choices[0].finish_reason
                    first_byte = time.time()
                    last_byte = first_byte  
                else:
                    response_body_text = response.get('body').read()
                    logger.log(logging.DEBUG, f'response_body_text={response_body_text}')
                    response_body = json.loads(response_body_text)
                    if _is_titan(modelId):
                        stop_reason = response_body['results'][0]['completionReason']
                    elif modelId.startswith('ai21'):
                        stop_reason = response_body['completions'][0]['finishReason']['reason']
                    else:
                        stop_reason = response_body['stop_reason']
                    first_byte = response['ResponseMetadata']['HTTPHeaders']['x-amzn-bedrock-invocation-latency']
                    last_byte = first_byte
                
                logger.log(logging.DEBUG, f"response body is {response_body}")
            elif stream:
                if _is_openai(modelId):
                    first_byte, stop_reason = consume_openai_stream(response)
                    last_byte = time.time()
                elif _is_claude3(modelId):
                    first_byte, last_byte, stop_reason = consume_bedrock_claude3_stream(response)
                elif not _is_openai(modelId):
                    first_byte, last_byte, stop_reason = consume_bedrock_stream_new(response)
            # verify we got all of the intended output tokens by verifying stop_reason
            valid_stop_reasons = ['max_tokens', 'length', 'LENGTH','stop_sequence','end_turn']
            assert stop_reason in valid_stop_reasons, f"stop_reason is {stop_reason} instead of 'max_tokens' or 'length', this means the model generated less tokens than required or stopped for a different reason."
            if _is_openai(modelId):
                duration_to_first_byte = first_byte - start
                duration_to_last_byte = last_byte - start
            else:
                duration_to_first_byte = float(first_byte)
                duration_to_last_byte = float(last_byte)
        except ClientError as err:
            if 'Thrott' in err.response['Error']['Code']:
                logger.log(logging.INFO, f'Got ThrottlingException. Sleeping {SLEEP_ON_THROTTLING_SEC} sec and retrying.')
                time.sleep(SLEEP_ON_THROTTLING_SEC)
                continue
            raise err
        break
    return duration_to_first_byte, duration_to_last_byte, invocation_timestamp_iso

'''
This method will benchmark the given scenarios.
scenarios - a list of scenarios to benchmark
scenario_config - a dictionary of configuration parameters
early_break - if true, will break after a single scenario, useful for debugging.
Returns a list of benchmarked scenarios with a list of invocation (latency and timestamp)
'''
def execute_benchmark(scenarios, scenario_config, early_break = False):
    scenarios = deepcopy(scenarios)
    pp = pprint.PrettyPrinter(indent=2)
    scenarios_list = []
    for scenario in scenarios:
        for i in range(scenario_config["invocations_per_scenario"]): # increase to sample each use case more than once to discover jitter
            scenario_label = f"{scenario['model_id']} in={scenario['in_tokens']}, out={scenario['out_tokens']}"
            logger.log(logging.INFO, f"About to execute scenario: [{scenario_label}")
            try:
                modelId = scenario['model_id']
                prompt = create_prompt(scenario['in_tokens'], modelId)
                
                if _is_openai(modelId):
                    client = OpenAI(
                        api_key = OPENAI_API_KEY
                    )
                else:
                    client = get_cached_client(scenario['region'], scenario['model_id'])
                time_to_first_token, time_to_last_token, timestamp = benchmark(client, modelId, prompt, scenario['out_tokens'], stream=scenario['stream'])

                if 'invocations' not in scenario: scenario['invocations'] = list()
                invocation = {
                    'time-to-first-token':  time_to_first_token,
                    'time-to-last-token':  time_to_last_token,
                    'timestamp_iso' : timestamp
                }
                scenario['invocations'].append(invocation)

                logger.log(logging.INFO, f"Scenario: [{scenario_label}, invocation: {pp.pformat(invocation)}")
                post_iteration(is_last_invocation = i == scenario_config["invocations_per_scenario"] - 1, scenario_config=scenario_config)
            except Exception as e:
                logger.log(logging.CRITICAL, f"Error is: {e}")
                logger.log(logging.CRITICAL, f"Error while processing scenario: {scenario_label}.")
            if early_break:
                break
        scenarios_list.append(scenario)
    logger.log(logging.INFO, f'scenarios at the end of execute benchmark is: {pp.pformat(scenarios_list)}')
    return scenarios_list


''' 
Get a boto3 bedrock runtime client for invoking requests
region - the AWS region to use
model_id_for_warm_up - the model id to warm up the client against, use None for no warmup
Note: Removing auto retries to ensure we're measuring a single transcation (e.g., in case of throttling).
'''
def _get_bedrock_client(region, model_id_for_warm_up = None):
    client = boto3.client(service_name='bedrock-runtime',
                          region_name=region,
                          config = botocore.config.Config(retries=dict(max_attempts=0))) 
    if model_id_for_warm_up:
        logger.log(logging.DEBUG, f"Calling benchmark for client warmup")
        benchmark(client, model_id_for_warm_up, create_prompt(50, model_id_for_warm_up), 1, stream=False)
    return client

'''
Get a possible cache client per AWS region 
region - the AWS region to use
model_id_for_warm_up - the model id to warm up the client against, use None for no warmup
'''
client_per_region={}
def get_cached_client(region, model_id_for_warm_up = None):
    logger.log(logging.DEBUG, f"get_cached_client called with region: {region}, model_id_for_warm_up: {model_id_for_warm_up}")
    if client_per_region.get(region) is None:
        client_per_region[region] = _get_bedrock_client(region, model_id_for_warm_up)
    return client_per_region[region]


def post_iteration(is_last_invocation, scenario_config):
    if scenario_config["sleep_between_invocations"] > 0 and not is_last_invocation:
        logger.log(logging.INFO, f'Sleeping for {scenario_config["sleep_between_invocations"]} seconds.')
        time.sleep(scenario_config["sleep_between_invocations"])
        
'''
This method draws a boxplot graph of each scenario.
scenarios - list of scenarios
title - title of the graph
metric - metric to be plotted (time-to-first-token or time-to-last-token)
'''
def graph_scenarios_boxplot(scenarios, title, metric = 'time-to-first-token', figsize=(10, 6)):
    import numpy as np
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=figsize)
    xlables = []
    
    # Angle labels if covering many scenarios, to avoid collisions
    if len(scenarios) > 4:
        x_ticks_angle=45
    else:
        x_ticks_angle=0

    for scenario in scenarios:
        invocations = [d[metric] for d in scenario['invocations']]
        percentile_95 = round(np.percentile(invocations, 95),2)
        percentile_99 = round(np.percentile(invocations, 99),2)
        xlables.append(f"{scenario['name']}\n(in={scenario['in_tokens']},out={scenario['out_tokens']}\np95={percentile_95}\np99={percentile_99}")
        ax.boxplot(invocations, positions=[scenarios.index(scenario)])

    ax.set_title(title)
    #ax.set_xticks(range(1, len(scenarios) + 1))
    ax.set_xticklabels(xlables, rotation=x_ticks_angle, ha="right")
    ax.set_ylabel(f'{metric} (sec)')
    ax.set_ylim(bottom=0) # Set y-axis to start at 0
    fig.tight_layout()
    plt.show()
    
def graph_scenarios_boxplot_save(scenarios, title, metric = 'time-to-first-token', figsize=(10, 6)):
    import numpy as np
    import matplotlib.pyplot as plt
    in_tokens = ''
    out_tokens = ''

    fig, ax = plt.subplots(figsize=figsize)
    xlables = []
    
    # Angle labels if covering many scenarios, to avoid collisions
    if len(scenarios) > 4:
        x_ticks_angle=45
    else:
        x_ticks_angle=0

    for scenario in scenarios:
        invocations = [d[metric] for d in scenario['invocations']]
        percentile_50 = round(np.percentile(invocations, 50),2)
        percentile_95 = round(np.percentile(invocations, 95),2)
        #percentile_99 = round(np.percentile(invocations, 99),2)
        #xlables.append(f"{scenario['name']}\n(in={scenario['in_tokens']},out={scenario['out_tokens']}\np95={percentile_95}\np99={percentile_99}")
        xlables.append(f"{scenario['name']}\n(in={scenario['in_tokens']},out={scenario['out_tokens']}\np50={percentile_50}\np95={percentile_95}")
        in_tokens = scenario['in_tokens']
        out_tokens = scenario['out_tokens']

        ax.boxplot(invocations, positions=[scenarios.index(scenario)])

    ax.set_title(title)
    #ax.set_xticks(range(1, len(scenarios) + 1))
    ax.set_xticklabels(xlables, rotation=x_ticks_angle, ha="right")
    ax.set_ylabel(f'{metric} (sec)')
    ax.set_ylim(bottom=0) # Set y-axis to start at 0
    fig.tight_layout()
    plt.savefig(f'title-{title},in-{in_tokens},out-{out_tokens},{metric}.png')
    plt.show()
    
def save_to_csv(title,result):
    import pandas as pd
    import numpy as np
    
    in_tokens = ''
    out_tokens = ''
    for index,scenario in enumerate(result):
        result[index]["label"] = scenario['model_id']+f"(in:{scenario['in_tokens']},out:{scenario['out_tokens']})"

        metric = 'time-to-first-token'
        invocations = [d[metric] for d in scenario['invocations']]
        percentile_50 = round(np.percentile(invocations, 50),2)
        percentile_95 = round(np.percentile(invocations, 95),2)
        percentile_99 = round(np.percentile(invocations, 99),2)
        #result1.append(f"{scenario['name']}\n(in={scenario['in_tokens']},out={scenario['out_tokens']}\np95={percentile_95}\np99={percentile_99}")
        result[index]["p50_first"] = percentile_50
        result[index]["p95_first"] = percentile_95
        result[index]["p99_first"] = percentile_95

        metric = 'time-to-last-token'
        invocations = [d[metric] for d in scenario['invocations']]
        percentile_50 = round(np.percentile(invocations, 50),2)
        percentile_95 = round(np.percentile(invocations, 95),2)
        percentile_99 = round(np.percentile(invocations, 99),2)
        #result1.append(f"{scenario['name']}\n(in={scenario['in_tokens']},out={scenario['out_tokens']}\np95={percentile_95}\np99={percentile_99}")
        result[index]["p50_last"] = percentile_50
        result[index]["p95_last"] = percentile_95
        result[index]["p99_last"] = percentile_99
        
        in_tokens = scenario['in_tokens']
        out_tokens = scenario['out_tokens']
        
    df = pd.DataFrame(result)
    column_list=["label", "p50_first", "p95_first", "p99_first", "p50_last", "p95_last","p99_last"]

    #filter the dataframe beforehand
    df[column_list].to_csv(f'title-{title},in-{in_tokens},out-{out_tokens}.csv',index=False)
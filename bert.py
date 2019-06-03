import numpy as np
import codecs
import json
import os
from keras.callbacks import ModelCheckpoint,EarlyStopping,LearningRateScheduler
import keras
from keras.layers import *
from keras.models import Model
import keras.backend as K
from keras.callbacks import Callback
from random import choice, sample
from keras_bert import get_model, load_model_weights_from_checkpoint
from tqdm import tqdm
from layers import Gate_Add_Lyaer,MaskedConv1D,MaskFlatten,MaskPermute,MaskRepeatVector
from utils import load_data,data_generator
os.environ['CUDA_VISIBLE_DEVICES'] = '0'

#bert path
config_path = '/home/ccit22/m_minbo/chinese_L-12_H-768_A-12/bert_config.json'
checkpoint_path = '/home/ccit22/m_minbo/chinese_L-12_H-768_A-12/bert_model.ckpt'
dict_path = '/home/ccit22/m_minbo/chinese_L-12_H-768_A-12/vocab.txt'
#input_path
train_data_path = 'inputs/train_data_me.json'
dev_data_path = 'inputs/dev_data_me.json'
test_data_path = 'inputs/test_data_me_train.json'
test_data_no_train_path = 'inputs/test_data_me_no_train.json'

event2id_path ='inputs/event2id.json'
char2id_path = 'inputs/all_chars_me.json'
#output_path
weight_name=  'models/baseline_bert_6_3.weights'
test_result_path = 'output/result_A.txt'
dev_result_path='output/result_dev.json' #dev_result用来做数据分
dev_bio_result_path = 'output/result_dev_bio.json' #dev_bio_result 用来观察抽取规则是否正确

id2char,char2id = json.load(open(char2id_path,encoding='utf-8'))
event2id = json.load(open(event2id_path,encoding='utf-8'))
train_data = json.load(open(train_data_path,encoding='utf-8'))
dev_data = json.load(open(dev_data_path,encoding='utf-8'))
test_data = json.load(open(test_data_path,encoding='utf-8'))
test_data_no_train = json.load(open(test_data_no_train_path,encoding='utf-8'))

#part of paramters
maxlen = 180
embedding_size = 300
hidden_size = 128
vocab_size = len(char2id)+2 #0pad 1 UNK
event_size = len(event2id)
event_embedding_size = 30
char_size = 128
debug = False

if debug==True:
    train_data = train_data[:2000]

def build_model_from_config(config_file,
                            checkpoint_file,
                            training=False,
                            trainable=False,
                            seq_len=None,
                            ):
    """Build the model from config file.

    :param config_file: The path to the JSON configuration file.
    :param training: If training, the whole model will be returned.
    :param trainable: Whether the model is trainable.
    :param seq_len: If it is not None and it is shorter than the value in the config file, the weights in
                    position embeddings will be sliced to fit the new length.
    :return: model and config
    """
    with open(config_file, 'r') as reader:
        config = json.loads(reader.read())
    if seq_len is not None:
        config['max_position_embeddings'] = min(seq_len, config['max_position_embeddings'])
    if trainable is None:
        trainable = training
    model = get_model(
        token_num=config['vocab_size'],
        pos_num=config['max_position_embeddings'],
        seq_len=config['max_position_embeddings'],
        embed_dim=config['hidden_size'],
        transformer_num=config['num_hidden_layers'],
        head_num=config['num_attention_heads'],
        feed_forward_dim=config['intermediate_size'],
        training=False,
        trainable=True,
    )
    inputs, outputs = model
    bio_label = Input(shape=(maxlen,))

    mask = Lambda(lambda x: K.cast(K.greater(K.expand_dims(x, 2), 0), 'float32'))(inputs[0])

    attention = TimeDistributed(Dense(1, activation='tanh'))(outputs)
    attention = MaskFlatten()(attention)
    attention = Activation('softmax')(attention)
    attention = MaskRepeatVector(config['hidden_size'])(attention)
    attention = MaskPermute([2, 1])(attention)
    sent_representation = multiply([outputs, attention])
    attention = Lambda(lambda xin: K.sum(xin, axis=1))(sent_representation)
    # lstm_attention = Lambda(seq_and_vec, output_shape=(None, self.hidden_size * 2))(
    #     [lstm, attention])  # [这里考虑下用相加的方法，以及门控相加]
    attention = MaskRepeatVector(maxlen)(attention)  # [batch,sentence,hidden_size]
    gate_attention = Gate_Add_Lyaer()([outputs, attention])
    gate_attention = Dropout(0.15)(gate_attention)

    cnn1 = MaskedConv1D(filters=hidden_size, kernel_size=3, activation='relu', padding='same')(gate_attention)
    #BIOE
    bio_pred = Dense(4, activation='softmax')(cnn1)

    entity_model = keras.models.Model([inputs[0], inputs[1]], [bio_pred])  # 预测subject的模型
    train_model = keras.models.Model([inputs[0], inputs[1],bio_label],[bio_pred])

    loss = K.sparse_categorical_crossentropy(bio_label, bio_pred)
    loss = K.sum(loss * mask[:, :, 0]) / K.sum(mask)

    train_model.add_loss(loss)
    train_model.summary()
    train_model.compile(
        optimizer=keras.optimizers.Adam(lr=3e-5),
    )
    load_model_weights_from_checkpoint(train_model, config, checkpoint_file, training)
    return train_model,entity_model

def extract_entity(bio_pred,data):
    #目前抽取规则变成了， 抽取出预测的所有可能的BIO，然后把文本长度最长的bio当作抽取目标
    #这样做的目的是，有些文本被预测成了单个B，这样明显是错误的。
    entities=[]
    text_in = []
    for _data in data:
        text = _data['text']
        text = '^' + text + '^'
        text_in.append(text)
    bio_pred = np.argmax(bio_pred,axis=-1) #[batch_size,sequence_length]
    for idx in range(len(data)):
        text = text_in[idx]
        bio = bio_pred[idx]
        flag = 0
        temps = []  # 保存所有可能被抽取出来的实体。 选择长度最长的哪一个当作预测目标
        for i in range(len(bio)):
            if bio[i] == 1: #找到B标识符1
                if i >= len(text): #预测出的1在文本范围外，来自pad的部分
                    break
                else:
                    entity = text[i]
                    for j in range(i+1,len(bio)):
                        if j >= len(text): #预测出的结构在文本范围外
                            entities.append(entity)
                            break
                        else:
                            if bio[j] == 2: #找到I标志符2
                                entity+=text[j]
                            else:
                                temps.append(entity)
                                break
        maxlen = 0
        _entity = ''
        for entity in temps:
            if len(entity) > maxlen:
                maxlen = len(entity)
                _entity = entity
        if _entity: #如果有抽取出来的目标，就append,
            entities.append(_entity)
            flag = 1
        if flag == 0:#没有预测出来补个空
            entities.append('')
    return entities

def comput_f1(entities):
    """
    对dev进行测评
    P_all = right_num_all / pred_num_all  # 准确率
    R_all = right_num_all / true_num_all  # 召回率
    F_all = 2 * P_all * R_all / (P_all + R_all)  # F值
    :param dev_file:
    :return:
    """
    right = 1e-10
    pred = 1e-10
    true = 1e-10
    for idx in range(len(dev_data)):
        pred_entity = entities[idx]
        true_entity = dev_data[idx]['entity']
        true += 1
        if pred_entity:
            pred+= 1
            if pred_entity == true_entity:
                right+=1
    P = right/pred
    R = right / true
    F = 2*P*R/(R+P)
    return P,R,F

def save_result(data,entities,mode):
    """
    按照  id , entites 写入文档
    :param data:
    :param entities:
    :return:
    """
    if mode == 'test':
        with open(test_result_path,'w',encoding='utf-8') as fr:
            for index in range(len(data)):
                id = data[index]['id']
                entity = entities[index]
                fr.write(str(id)+','+str(entity)+'\n')
            for data in test_data_no_train:
                id = data['id']
                entity = 'NaN'
                fr.write(str(id)+','+str(entity)+'\n')
    else:
            dev_result = []
            for index in range(len(data)):
                dic = {}
                id = data[index]['id']
                text = data[index]['text']
                event_type = data[index]['event_type']
                entity = entities[index]
                dic['id'] = id
                dic['text'] = text
                dic['entity'] = entity
                dic['event_type'] = event_type
                dev_result.append(dic)
            with codecs.open(dev_result_path, 'w', encoding='utf-8') as f:
                json.dump(dev_result, f, indent=4, ensure_ascii=False)

# def save_bio_pred(bio_pred,data,entities):
#     """
#     将bio_pred保存输出，看下抽取规则有没有地方可以改进
#     :param bio_pred:
#     :return:
#     """
#     bio_pred = np.argmax(bio_pred) #[batch,sentence]
#     dev_bio_result = []
#     with open(test_result_path, 'w', encoding='utf-8') as fr:
#         for index in range(len(data)):
#             dic = {}
#             dic['id'] = data[index]['id']
#             dic['text'] = data[index]['text']
#             dic['event_type'] = data[index]['event_type']
#             dic['entities'] = entities[index]
#             dic['bio_pred'] = bio_pred[index]
#             dev_bio_result.append(dic)
#         with codecs.open(dev_bio_result_path, 'w', encoding='utf-8') as f:
#             json.dump(dev_bio_result, f, indent=4, ensure_ascii=False)

def predict_test_batch(mode):
    if mode == 'test':
        weight_file = weight_name
        train_model.load_weights(weight_file)
        test_BERT_INPUT0, test_BERT_INPUT1 = load_data(test_data,'test')
        bio_pred =entity_model.predict([test_BERT_INPUT0, test_BERT_INPUT1],batch_size=1000,verbose=1) #[batch_size,sentence,num_classes]
        entites = extract_entity(bio_pred,test_data)
        save_result(test_data,entites,'test')
    else:
        #对dev进行测评
        # weight_file = weight_name
        # train_model.load_weights(weight_file)
        dev_BERT_INPUT0, dev_BERT_INPUT1,_ = load_data(dev_data,'dev')
        bio_pred =entity_model.predict([dev_BERT_INPUT0, dev_BERT_INPUT1],batch_size=1000,verbose=1) #[batch_size,sentence,num_classes]
        entites = extract_entity(bio_pred,dev_data)
        save_result(dev_data,entites,'dev') #dev的entity预测结果
        # save_bio_pred(bio_pred,dev_data,entites) #dev的bio预测结果
        return comput_f1(entites)

def scheduler(epoch):
    # 每隔1个epoch，学习率减小为原来的1/2
    # if epoch % 100 == 0 and epoch != 0:
    #再epoch > 3的时候,开始学习率递减,每次递减为原来的1/2,最低为2e-6
    if epoch+1 <= 2:
        return K.get_value(train_model.optimizer.lr)
    else:
        lr = K.get_value(train_model.optimizer.lr)
        lr = lr*0.5
        if lr < 2e-6:
            return 2e-6
        else:
            return lr
####################################################################################################################
train_model,entity_model = build_model_from_config(config_path, checkpoint_path, seq_len=180)
train_D = data_generator(train_data,26)
reduce_lr = LearningRateScheduler(scheduler, verbose=1)
best_f1 = 0
for i in range(1,15):
    train_model.fit_generator(train_D.__iter__(),
                              steps_per_epoch=len(train_D),
                              epochs=1,
                              callbacks=[reduce_lr]
                              )
    # if (i) % 2 == 0 : #两次对dev进行一次测评,并对dev结果进行保存
    # print('进入到这里了哟~')
    P, R, F = predict_test_batch('dev')
    if F > best_f1 :
        best_f1 = F
        train_model.save_weights(weight_name)
        print('当前第{}个epoch，准确度为{},召回为{},f1为：{}'.format(i,P,R,F))
predict_test_batch('test')
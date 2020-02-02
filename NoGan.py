import os
import datetime
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision import utils
from torch.utils.tensorboard import SummaryWriter

from utils import image_gradient, tensor_restore, ProgressBar, DatasetDefiner
from AnomaNet import GTNet
from AnomaNet import AnomaNet as Generator

LEN_ZFILL = 5


# Typical training and evaluation schemes without GAN
class NoGAN(object):
    def __init__(self, name, im_size, store_path, device_str=None):
        self.name = name
        self.im_size = im_size
        # paths
        self.store_path = store_path
        self.input_store_path = self.store_path + "/inputs"             # data for training and evaluation
        self.training_data_file = os.path.join(self.input_store_path, self.name + "_training.pt")
        self.evaluation_data_file = os.path.join(self.input_store_path, self.name + "_evaluation.pt")
        self.model_store_path = self.store_path + "/models"             # trained models
        self.gen_image_store_path = self.store_path + "/gen_images"     # generated images (for visual checking)
        self.output_store_path = self.store_path + "/outputs"           # outputs for evaluation
        self.log_path = self.store_path + "/log"                        # tensorboard log
        self._create_all_paths()
        # device
        if device_str is None:
            self.device = torch.device("cuda:0" if torch.cuda.is_available()
                                       else "cpu")
        else:
            assert isinstance(device_str, str)
            self.device = torch.device(device_str)

        print("ContextNet init...")
        self.ContextNet = GTNet(self.im_size, self.device)
        self.ContextNet.eval()

        print("Anomanet init...")
        self.AnomaNet = Generator(self.im_size, self.device)

        # ADAM optimizer
        self.learning_rate = 1e-4
        self.optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.AnomaNet.parameters()), lr=self.learning_rate)

        # Set the logger
        self.logger = SummaryWriter(self.log_path)
        self.logger.flush()

    # create necessary directories
    def _create_all_paths(self):
        self._create_path(self.input_store_path)
        self._create_path(self.model_store_path)
        self._create_path(self.gen_image_store_path)
        self._create_path(self.output_store_path)
        self._create_path(self.log_path)

    def _create_path(self, path):
        if not os.path.exists(path):
            os.makedirs(path)

    # load pretrained model and optimizer
    def _load_model(self, model_filename):
        self.AnomaNet.load_state_dict(torch.load(os.path.join(self.model_store_path, model_filename)))
        print("AnomaNet loaded from %s" % model_filename)

    # save pretrained model and optimizer
    def _save_model(self, model_filename):
        torch.save(self.AnomaNet.state_dict(), os.path.join(self.model_store_path, model_filename))
        print("AnomaNet saved to %s" % model_filename)

    # dataset: instance of DataHelper
    # store_path: path for storing trained model files (should contain dataset name)
    def train(self, epoch_start, epoch_end, batch_size=16, save_every_x_epochs=5):
        # set mode for networks
        self.ContextNet.eval()
        self.AnomaNet.train()
        if epoch_start > 0:
            self._load_model("model_epoch_%s.pkl" % str(epoch_start).zfill(LEN_ZFILL))

        # turn on debugging related to gradient
        torch.autograd.set_detect_anomaly(True)

        # create data loader for yielding batches
        dataset = DatasetDefiner(self.name, self.im_size, batch_size)
        dataset.load_training_data(out_file=self.training_data_file)
        dataset = dataset.get_attribute("training_data")
        dataloader = torch.utils.data.DataLoader(dataset, 1, shuffle=True)  # batchsize = 1
        total_batch_num = dataset.get_num_of_batchs()

        # progress bar
        progress = ProgressBar(total_batch_num * (epoch_end - epoch_start), fmt=ProgressBar.FULL)
        print("Started time:", datetime.datetime.now())

        # variables for logging
        imgs, out_reconstruction, out_instant_pred, out_longterm_pred = None, None, None, None

        # training for each epoch
        for epoch in range(epoch_start, epoch_end):
            iter_count = 0

            # process long video
            for video_idx, clip_indices in dataloader:
                self.AnomaNet.zero_grad()

                # process short clip
                for clip_index in clip_indices:
                    assert len(clip_index) == 2
                    imgs = dataset.data[video_idx][clip_index[0]:clip_index[1]]
                    imgs.unsqueeze_(0)
                    imgs = imgs.to(self.device)
                    _, _, in_context, out_reconstruction, out_instant_pred, out_longterm_pred = self.AnomaNet(imgs[:, :-1])  # skip last frame
                    gt_context = self.ContextNet(imgs[0])
                    gt_context = gt_context.unsqueeze_(0)

                    # define loss functions, may be different for partial losses
                    L2_loss, L1_loss = nn.MSELoss(), nn.L1Loss()

                    # context loss
                    context_loss = L2_loss(in_context, gt_context[:, :-1])

                    # prediction losses
                    dx_instant_pred, dy_instant_pred = image_gradient(out_instant_pred, out_abs=True)
                    dx_longterm_pred, dy_longterm_pred = image_gradient(out_longterm_pred, out_abs=True)
                    dx_input, dy_input = image_gradient(imgs[:, 1:], out_abs=True)
                    instant_loss = L2_loss(out_instant_pred, imgs[:, 1:]) + \
                        L1_loss(dx_instant_pred, dx_input) + L1_loss(dy_instant_pred, dy_input)
                    longterm_loss = L2_loss(out_longterm_pred, imgs[:, 1:]) + \
                        L1_loss(dx_longterm_pred, dx_input) + L1_loss(dy_longterm_pred, dy_input)

                    # reconstruction loss
                    dx_recons_pred, dy_recons_pred = image_gradient(out_reconstruction, out_abs=True)
                    dx_input, dy_input = image_gradient(imgs[:, :-1], out_abs=True)
                    reconst_loss = L2_loss(out_reconstruction, imgs[:, :-1]) + L1_loss(dx_recons_pred, dx_input) + L1_loss(dy_recons_pred, dy_input)

                    # total loss
                    loss_weights = {"context": 1, "recons": 1, "instant": 1, "longterm": 1}
                    loss = loss_weights["context"]*context_loss + loss_weights["recons"]*reconst_loss + \
                        loss_weights["instant"]*instant_loss + loss_weights["longterm"]*longterm_loss

                    # back-propagation
                    loss.backward()
                    self.optimizer.step()

                    # emit losses for visualization
                    msg = " [context = %3.4f, recons = %3.4f, instant = %3.4f, longterm = %3.4f]" \
                          % (context_loss.item(), reconst_loss.item(), instant_loss.item(), longterm_loss.item())

                    progress.current += 1
                    progress(msg)

                    # print("epoch %s/%d -> iter %s/%d: context = %3.4f, recons = %3.4f, instant = %3.4f, longterm = %3.4f"
                    #       % (str(epoch + 1).zfill(len(str(epoch_end))), epoch_end,
                    #          str(iter_count).zfill(len(str(total_batch_num))), total_batch_num,
                    #          context_loss.item(), reconst_loss.item(), instant_loss.item(), longterm_loss.item()))

                    # ============ TensorBoard logging ============#
                    # Log the scalar values
                    info = {
                       'Loss context': context_loss.item(),
                       'Loss reconst': reconst_loss.item(),
                       'Loss instant': instant_loss.item(),
                       'Loss longterm': longterm_loss.item()
                    }
                    idx = epoch * total_batch_num + iter_count
                    for tag, value in info.items():
                        self.logger.add_scalar(tag, value, idx)

                    iter_count += 1

            # Saving model and sampling images every X epochs
            if (epoch + 1) % save_every_x_epochs == 0:
                self._save_model("model_epoch_%s.pkl" % str(epoch + 1).zfill(LEN_ZFILL))

                # Denormalize images and save them in grid 8x8
                images_to_save = [[tensor_restore(imgs.data.cpu()[0][0]),
                                   tensor_restore(out_reconstruction.data.cpu()[0][0]),
                                   tensor_restore(imgs.data.cpu()[0][1]),
                                   tensor_restore(out_instant_pred.data.cpu()[0][0]),
                                   tensor_restore(out_longterm_pred.data.cpu()[0][0])],
                                  [tensor_restore(imgs.data.cpu()[0][-2]),
                                   tensor_restore(out_reconstruction.data.cpu()[0][-1]),
                                   tensor_restore(imgs.data.cpu()[0][-1]),
                                   tensor_restore(out_instant_pred.data.cpu()[0][-1]),
                                   tensor_restore(out_longterm_pred.data.cpu()[0][-1])]]
                images_to_save = [utils.make_grid(images, nrow=1) for images in images_to_save]
                grid = utils.make_grid(images_to_save, nrow=2)
                utils.save_image(grid, "%s/gen_epoch_%s.png" % (self.gen_image_store_path, str(epoch + 1).zfill(LEN_ZFILL)))

        # finish iteration
        progress.done()
        print("Finished time:", datetime.datetime.now())

        # Save the trained parameters
        if (epoch + 1) % save_every_x_epochs != 0:  # not already saved inside loop
            self._save_model("model_epoch_%s.pkl" % str(epoch + 1).zfill(LEN_ZFILL))

    def infer(self, epoch, batch_size=16, data_set="test_set"):
        assert data_set in ("test_set", "training_set")
        # load pretrained model and set to eval() mode
        self._load_model("model_epoch_%s.pkl" % str(epoch).zfill(LEN_ZFILL))
        self.AnomaNet.eval()

        # dataloader for yielding batches
        dataset = DatasetDefiner(self.name, self.im_size, batch_size)
        if data_set == "test_set":
            dataset.load_evaluation_data(out_file=self.evaluation_data_file)
            dataset = dataset.get_attribute("evaluation_data")
        else:
            dataset.load_training_data(out_file=self.training_data_file)
            dataset = dataset.get_attribute("training_data")

        dataloader = torch.utils.data.DataLoader(dataset, 1, shuffle=False)  # batchsize = 1
        total_batch_num = dataset.get_num_of_batchs()

        # init variables for batch evaluation
        results_reconst, results_instant, results_longterm = [], [], []

        # progress bar
        progress = ProgressBar(total_batch_num, fmt=ProgressBar.FULL)
        print("Started time:", datetime.datetime.now())

        # get data info for each whole video
        for video_idx, clip_indices in dataloader:
            output_reconst, output_instant, output_longterm = [], [], []

            # evaluate a batch
            for clip_idx in clip_indices:
                assert len(clip_idx) == 2
                imgs = dataset.data[video_idx][clip_idx[0]:clip_idx[1]]
                imgs.unsqueeze_(0)
                imgs = imgs.to(self.device)
                _, _, _, out_reconstruction, out_instant_pred, out_longterm_pred = self.G(imgs[:, :-1])  # skip last frame

                # store results
                output_reconst.append(out_reconstruction[0])
                output_instant.append(out_instant_pred[0])
                output_longterm.append(out_longterm_pred[0])

                progress.current += 1
                progress()

            results_reconst.append(torch.cat(output_reconst, dim=0))
            results_instant.append(torch.cat(output_instant, dim=0))
            results_longterm.append(torch.cat(output_longterm, dim=0))

        progress.done()
        print("Finished time:", datetime.datetime.now())

        # store data to file
        data = {"reconst": results_reconst,
                "instant": results_instant,
                "longterm": results_longterm}
        out_file = self.output_store_path + '/out_epoch_%s_data_%s.pt' % (str(epoch).zfill(LEN_ZFILL), data_set)
        torch.save(data, out_file)
        print("Data saved to %s" % out_file)

    def evaluate(self, epoch):
        # define function for computing anomaly score
        # input tensor shape: (n, C, H, W)
        # power: used for combining channels (1=abs, 2=square)
        def calc_score(tensor, power=1, patch_size=5):
            assert power in (1, 2) and patch_size % 2
            # combine channels
            tensor2 = torch.sum(torch.abs(tensor) if power == 1 else tensor**2, dim=1)
            # convolution for most salient patch
            weight = torch.ones(1, 1, patch_size, patch_size)
            padding = patch_size // 2
            heatmaps = [F.conv2d(item, weight, stride=1, padding=padding).cpu().numpy() for item in tensor2]
            # get sum value and position of the patch
            scores = [np.max(heatmap) for heatmap in heatmaps]
            positions = [np.where(heatmap == np.max(heatmap)) for heatmap in heatmaps]
            positions = [(position[0][0], position[1][0]) for position in positions]
            # return scores and positions
            return {"score": scores, "position": positions}

        # load real data
        eval_data_list = torch.load(self.evaluation_data_file)
        assert isinstance(eval_data_list, (list, tuple))
        print("Eval data shape:", [video.shape for video in eval_data_list])

        # load outputted data
        output_file = self.output_store_path + '/out_epoch_%s_data_test_set.pt' % str(epoch).zfill(LEN_ZFILL)
        outputs = torch.load(output_file)
        assert isinstance(outputs, dict)
        reconst_list, instant_list, longterm_list = outputs["reconst"], outputs["instant"], outputs["longterm"]
        assert isinstance(reconst_list, (list, tuple))
        assert isinstance(instant_list, (list, tuple))
        assert isinstance(longterm_list, (list, tuple))

        # evaluation
        assert len(eval_data_list) == len(reconst_list) == len(instant_list) == len(longterm_list)
        # torch.tensor([torch.max(score) for score in torch.abs(out_reconstruction[0] - imgs[0, :-1])])
        reconst_diff = [reconst_list[i] - eval_data_list[i][:-1] for i in range(len(eval_data_list))]
        instant_diff = [instant_list[i] - eval_data_list[i][1:] for i in range(len(eval_data_list))]
        longterm_diff = [longterm_list[i] - eval_data_list[i][1:] for i in range(len(eval_data_list))]

        # compute patch scores and localize positions -> list of dicts
        # temporary: get only scores
        reconst_patches = [calc_score(tensor)["score"] for tensor in reconst_diff]
        instant_patches = [calc_score(tensor)["score"] for tensor in instant_diff]
        longterm_patches = [calc_score(tensor)["score"] for tensor in longterm_diff]

        # return auc(s)
        dataset = DatasetDefiner(self.name, self.im_size, -1)
        return dataset.evaluate(reconst_patches), dataset.evaluate(instant_patches), dataset.evaluate(longterm_patches)

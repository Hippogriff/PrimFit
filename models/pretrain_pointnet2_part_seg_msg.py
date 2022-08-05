import torch.nn as nn
import torch
import torch.nn.functional as F
from models.pointnet_util import PointNetSetAbstractionMsg,PointNetSetAbstraction,PointNetFeaturePropagation
import numpy as np
from convex_loss import convex_loss
import sys
from models.reconstruction import AtlasNet, ChamferDistance


class get_model(nn.Module):
    def __init__(self, num_parts, normal_channel=False, l2_norm=False, reconstruct=False):
        super(get_model, self).__init__()
        if normal_channel:
            additional_channel = 3
        else:
            additional_channel = 0
        self.normal_channel = normal_channel
        self.l2_norm = l2_norm

        self.beta = 1

        self.sa1 = PointNetSetAbstractionMsg(512, [0.1, 0.2, 0.4], [32, 64, 128], 3+additional_channel, [[32, 32, 64], [64, 64, 128], [64, 96, 128]])
        self.sa2 = PointNetSetAbstractionMsg(128, [0.4,0.8], [64, 128], 128+128+64, [[128, 128, 256], [128, 196, 256]])
        self.sa3 = PointNetSetAbstraction(npoint=None, radius=None, nsample=None, in_channel=512 + 3, mlp=[256, 512, 1024], group_all=True)
        self.fp3 = PointNetFeaturePropagation(in_channel=1536, mlp=[256, 256])
        self.fp2 = PointNetFeaturePropagation(in_channel=576, mlp=[256, 128])
        self.fp1 = PointNetFeaturePropagation(in_channel=150 + additional_channel, mlp=[128, 128])
        self.conv1 = nn.Conv1d(128, 128, 1)
        self.bn1 = nn.BatchNorm1d(128)
        self.drop1 = nn.Dropout(0.5)
        self.conv2 = nn.Conv1d(128, num_parts, 1)
        self.extra_conv_emb = nn.Conv1d(128, 128, 1)
        self.reconstruct = reconstruct
        if self.reconstruct:
            self.atlasnet = AtlasNet()
            self.chamferdistance = ChamferDistance()

    def forward(self, xyz, cls_label, chamfer_points=0, include_convex_loss=False, if_cuboid=False,  include_intersect_loss=False, include_entropy_loss=False, include_pruning=False, quantile=0.01, msc_iterations=5, max_num_clusters=25, visualize=False, seed=0, batch_id=0, class_list=[], epoch=-1, alpha=1, beta=1, evaluation=False):
        # Set Abstraction layers
        B,C,N = xyz.shape
        
        if self.normal_channel:
            l0_points = xyz
            l0_xyz = xyz[:,:3,:]
        else:
            l0_points = xyz
            l0_xyz = xyz
        l1_xyz, l1_points = self.sa1(l0_xyz, l0_points)
        l2_xyz, l2_points = self.sa2(l1_xyz, l1_points)
        l3_xyz, l3_points = self.sa3(l2_xyz, l2_points)
        # Feature Propagation layers       
        l2_points = self.fp3(l2_xyz, l3_xyz, l2_points, l3_points)
        l1_points = self.fp2(l1_xyz, l2_xyz, l1_points, l2_points)
        cls_label_one_hot = cls_label.view(B, 16, 1).repeat(1, 1, N)
        l0_points = self.fp1(l0_xyz, l1_xyz, torch.cat([cls_label_one_hot,l0_xyz,l0_points],1), l1_points)        
        # FC layers
        feat = F.relu(self.bn1(self.conv1(l0_points)))
       
        if include_convex_loss:
            if self.beta > 0.001:
                self.beta *= 0.99
            else:
                include_entropy_loss = False
            feat_embed = feat
            feat_embed = self.extra_conv_emb(feat_embed)
                
            if self.l2_norm:
                #print('Normalizing......')
                feat_embed = F.normalize(feat_embed, p=2, dim=1)
            #feat_embed = F.normalize(feat_embed, p=2, dim=1)

            #feat_embed.register_hook(lambda p: print('Norm: ', p.norm())) 
            total_loss, chamfer_loss = convex_loss(xyz, chamfer_points, feat_embed, if_cuboid=if_cuboid, quantile=quantile, include_pruning=include_pruning, include_intersect_loss=include_intersect_loss, include_entropy_loss=include_entropy_loss, iterations=msc_iterations, max_num_clusters=max_num_clusters, visualize=visualize, seed=seed, batch_id=batch_id, epoch=epoch, class_list=class_list, alpha=alpha, beta=self.beta, evaluation=evaluation)
        elif self.reconstruct:
            z = feat.mean(dim=2)
            output_points = self.atlasnet(z)
            total_loss = self.chamferdistance(output_points, xyz.permute(0, 2, 1))
            chamfer_loss = torch.zeros(1).cuda()
        else:
            total_loss = torch.zeros(1).cuda()
            chamfer_loss = torch.zeros(1).cuda()

        x = self.drop1(feat)
        x = self.conv2(x)
        x = F.log_softmax(x, dim=1)        
        x = x.permute(0, 2, 1)
        return x, (l1_points, l2_points, l3_points), feat, total_loss, chamfer_loss


class get_loss(nn.Module):
    def __init__(self):
        super(get_loss, self).__init__()

    def forward(self, pred, target, trans_feat):
        #total_loss = F.nll_loss(pred, target)
        total_loss = F.cross_entropy(pred, target)
        return total_loss


class get_selfsup_loss(nn.Module):
    def __init__(self, margin=0.5):
        super(get_selfsup_loss, self).__init__()
        self.margin = margin

    def forward(self, feat, target):
        feat = F.normalize(feat, p=2, dim=1)
        pair_sim = torch.bmm(feat.transpose(1,2), feat)

        one_hot_target = F.one_hot(target).float()
        pair_target = torch.bmm(one_hot_target, one_hot_target.transpose(1,2))        

        cosine_loss = pair_target * (1. - pair_sim) + (1. - pair_target) * F.relu(pair_sim - self.margin)
        diag_mask = 1 - torch.eye(cosine_loss.shape[-1])  # discard diag elems (always 1)

        with torch.no_grad():
            # balance positive and negative pairs
            pos_fraction = (pair_target.data == 1).float().mean()
            sample_neg = torch.cuda.FloatTensor(*pair_target.shape).uniform_() > 1 - pos_fraction
            sample_mask = (pair_target.data == 1) | sample_neg # all positives, sampled negatives

        cosine_loss = diag_mask.unsqueeze(0).cuda() * sample_mask.type(torch.cuda.FloatTensor) * cosine_loss 
        total_loss = 0.5 * cosine_loss.mean() # scale down

        return total_loss
